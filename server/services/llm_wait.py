"""Bounded transient projection of versioned model-attempt status."""
from __future__ import annotations

import time
from collections import OrderedDict

_REQUIRED = ("workflow_id", "node_id", "execution_key", "request_id", "execution_id", "root_execution_id")
_OPTIONAL = ("generation", "workspace_epoch", "invocation_id", "thread_id", "turn_id", "agent_node_id", "agent_label",
             "activity_id", "attempt", "provider", "model")


def validate_wait(value):
    if not isinstance(value, dict) or type(value.get("runtime_behavior_version")) is not int or value["runtime_behavior_version"] != 1:
        return None
    if any(not isinstance(value.get(key), str) or not 0 < len(value[key]) <= 256 for key in _REQUIRED):
        return None
    if value.get("scope") not in {"generation", "workspace"} or value.get("state") not in {"waiting", "cleared"}:
        return None
    if type(value.get("seq")) is not int or value["seq"] < 1 or type(value.get("elapsed_ms")) is not int or value["elapsed_ms"] < 0:
        return None
    from services.llm.observation import _STAGES
    if value.get("stage") not in _STAGES:
        return None
    result = {key: value[key] for key in (*_REQUIRED, "runtime_behavior_version", "scope", "state", "seq", "elapsed_ms", "stage")}
    for key in _OPTIONAL:
        if key not in value:
            continue
        item = value.get(key)
        if key in {"generation", "workspace_epoch", "attempt"}:
            if type(item) is not int or item < (1 if key == "attempt" else 0):
                return None
        elif not isinstance(item, str) or not 0 < len(item) <= 256:
            return None
        result[key] = item
    return result


class LLMWaitRegistry:
    def __init__(self, *, limit=4096, ttl=3600, clock=time.monotonic):
        self.limit, self.ttl, self.clock = limit, ttl, clock
        self.records = OrderedDict()
        self.closed = OrderedDict()

    def _prune(self):
        now = self.clock()
        for mapping in (self.records, self.closed):
            for key, (updated, _) in list(mapping.items()):
                if now - updated > self.ttl:
                    mapping.pop(key, None)
            while len(mapping) > self.limit:
                mapping.popitem(last=False)

    def apply(self, raw):
        self._prune()
        item = validate_wait(raw)
        if item is None:
            return None
        prefix = (item["workflow_id"], item["node_id"])
        fences = [(*prefix, item["execution_id"]), (*prefix, item["execution_key"])]
        if item["scope"] == "generation" and "generation" in item:
            fences.append((item["workflow_id"], "generation", str(item["generation"])))
        if item.get("invocation_id"):
            fences.append((item["workflow_id"], "invocation", item["invocation_id"]))
        if item["scope"] == "workspace" and "workspace_epoch" in item:
            fences.append((item["workflow_id"], "workspace_epoch", str(item["workspace_epoch"])))
        if any(key in self.closed for key in fences):
            return None
        key = (*prefix, item["execution_key"], item["request_id"])
        previous = self.records.get(key)
        if previous and (previous[1]["seq"] >= item["seq"] or previous[1]["state"] == "cleared"):
            return None
        self.records[key] = (self.clock(), item)
        self.records.move_to_end(key)
        self._prune()
        return item

    def close(self, workflow_id, *, node_id=None, execution_id=None, execution_key=None, root_execution_id=None,
              generation=None, scope=None, invocation_id=None, workspace_epoch=None):
        cleared = []
        self._prune()
        for key, (_, value) in list(self.records.items()):
            if value["workflow_id"] != workflow_id or node_id and value["node_id"] != node_id:
                continue
            if scope and value["scope"] != scope:
                continue
            if execution_id and value["execution_id"] != execution_id:
                continue
            if execution_key and value["execution_key"] != execution_key:
                continue
            if root_execution_id and value["root_execution_id"] != root_execution_id:
                continue
            if generation is not None and value.get("generation") != generation:
                continue
            if invocation_id and value.get("invocation_id") != invocation_id:
                continue
            if workspace_epoch is not None and value.get("workspace_epoch") != workspace_epoch:
                continue
            if not any((execution_id, execution_key, root_execution_id, generation is not None, invocation_id, workspace_epoch is not None)):
                continue
            self.closed[(*key[:2], value["execution_key"])] = (self.clock(), True)
            if value["state"] == "waiting":
                delta = {**value, "state": "cleared", "seq": value["seq"] + 1}
                self.records[key] = (self.clock(), delta)
                cleared.append(delta)
        if node_id and execution_id:
            self.closed[(workflow_id, node_id, execution_id)] = (self.clock(), True)
        if node_id and execution_key:
            self.closed[(workflow_id, node_id, execution_key)] = (self.clock(), True)
        if generation is not None and scope == "generation":
            self.closed[(workflow_id, "generation", str(generation))] = (self.clock(), True)
        if invocation_id:
            self.closed[(workflow_id, "invocation", invocation_id)] = (self.clock(), True)
        if workspace_epoch is not None and scope == "workspace":
            self.closed[(workflow_id, "workspace_epoch", str(workspace_epoch))] = (self.clock(), True)
        self._prune()
        return cleared

    def resume_generation(self, workflow_id, generation):
        self.closed.pop((workflow_id, "generation", str(generation)), None)
        for _, item in self.records.values():
            if item["workflow_id"] == workflow_id and item["scope"] == "generation" and item.get("generation") == generation:
                self.closed.pop((workflow_id, item["node_id"], item["execution_key"]), None)

    def snapshot(self):
        self._prune()
        return [item for _, item in self.records.values()]


async def close_diagnostic_waits(workflow_id, **scope):
    """Optional projection cleanup must not prevent Stop/Reset itself."""
    try:
        from services.status_broadcaster import get_status_broadcaster
        await get_status_broadcaster().close_llm_waits(workflow_id, **scope)
    except Exception:
        try:
            from core.logging import get_logger
            get_logger(__name__).debug("llm_wait_cleanup_unavailable")
        except Exception:
            pass

# Process Manager

Read the trusted runtime context before starting a process. It identifies the host platform, workflow root, per-tool default cwd, Python executables and process-list scope. Commands run as executable arguments, not shell expressions; do not assume Bash or translate Windows commands blindly.

Omitted `cwd` uses this Process Manager tool's own directory. For artifacts shared with file tools, use `cwd: "."` to select the workflow root, another explicit workflow-relative cwd, or an allowed absolute directory. Verify the files exist in the effective directory before starting a server.

Declare every listener port in `ports`. On `PORT_IN_USE`, inspect the structured ownership classification. `list` shows only managed processes in this workflow on this backend worker; an empty result is not an inventory of OS listeners.

Stop or restart a named current-workflow process only when the task authorizes it. Never kill an unrelated PID. For another workflow's or unknown listener, select a different permitted port when the task allows, or ask the owner to resolve it.

Use `get_output` and returned process details to verify startup. The actual shell tool chooses the installed Nushell or host shell and has its own workspace-rooted cwd; it is not a separate no-PATH sandbox. PythonExecutor runs inside the backend process.

The browser's `localhost` is the browser-owner host. Confirm process/browser co-location before opening a loopback development URL; otherwise use an explicitly reachable URL.

A five-second model-wait notice is informational. It does not stop the request or authorize another process start.

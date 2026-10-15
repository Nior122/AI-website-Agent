"""Local verifier and graph runner for exported n8n workflow JSON files.

Because this environment has no live n8n binary/Node.js runtime, the runner parses the actual exported
n8n workflow JSON definitions (`nodes`, `connections`, `parameters`, and `={{ ... }}` expressions) and
executes their HTTP requests over loopback (`127.0.0.1`) against the running `orchestration.api` server.
It is a *verifier*, not an n8n host: nothing is scheduled, activated, or retried here.

Two rules keep the verifier safe to point at a real workspace:

- **Loopback only.** Every HTTP node carries the API bearer token in a header, so a workflow that names
  a non-loopback host is refused before any request is made - a copied or hand-edited export can never
  walk the credential off the machine. `allow_remote=True` exists for an operator who deliberately wants
  that and is explicit about it.
- **Failures are answers, not crashes.** A refused connection, a timeout, an HTTP error status, and a
  body that is not JSON each become a structured output on the failing node (`error`, `error_kind`,
  `error_code`, `http_status`, `status: "FAILED"`), so the workflow's own IF branches reach their
  documented outcome node instead of the run dying with a traceback. The collected problems are also
  reported once on the result as `execution_status` / `errors`.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import socket
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

#: Hosts a workflow may address. The runner sends the API bearer token with every request, so anything
#: outside the machine is a policy refusal rather than a runtime failure.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

#: Seconds a single workflow HTTP node waits before the run records a timeout.
N8N_REQUEST_TIMEOUT_SECONDS = 15


class N8nWorkflowTargetRefused(ValueError):
    """A workflow node addresses a host this local verifier must not contact."""


def _target_host(url: str) -> str:
    try:
        parsed = urlsplit(str(url))
    except ValueError as error:  # unparseable URL is a workflow-authoring error, said plainly
        raise N8nWorkflowTargetRefused(f"Workflow URL is not parseable: {url!r}.") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise N8nWorkflowTargetRefused(
            f"Workflow URL must be an absolute http(s) URL addressed to this machine, got {url!r}. "
            "Set AI_WEBSITE_AGENT_API_URL to something like http://127.0.0.1:8787."
        )
    return parsed.hostname


def _assert_loopback_target(url: str, *, allow_remote: bool) -> str:
    host = _target_host(url)
    if not allow_remote and host not in LOOPBACK_HOSTS:
        raise N8nWorkflowTargetRefused(
            f"Refusing to run a workflow node against non-loopback host {host!r}: this local verifier "
            "sends the API bearer token with every request. Point AI_WEBSITE_AGENT_API_URL at "
            "http://127.0.0.1:<port>, or pass allow_remote=True deliberately."
        )
    if parsed_port := urlsplit(str(url)).port:
        if not (1 <= parsed_port <= 65535):
            raise N8nWorkflowTargetRefused(f"Workflow URL has an invalid port: {parsed_port}.")
    return host


def _describe_failure(error: Exception, *, method: str, url: str, timeout_seconds: int) -> dict[str, Any]:
    """Turn a transport-level exception into the structured answer a workflow can branch on."""
    parsed = urlsplit(str(url))
    target = f"{parsed.netloc}{parsed.path}"
    hint = ""
    if isinstance(error, HTTPError):
        kind = "http_error"
        message = f"{method} {target} returned HTTP {error.code}."
        hint = "Read the response 'error'/'error_code' fields; they name the workflow condition to fix."
    elif isinstance(error, (socket.timeout, TimeoutError)):
        kind = "timeout"
        message = f"{method} {target} did not answer within {timeout_seconds}s."
        hint = ("The API may be busy or wedged - check GET /healthz and the worker log before re-running; "
                "never assume the request had no effect when it mutates state.")
    elif isinstance(error, URLError):
        kind = "connection_failed"
        reason = getattr(error, "reason", error)
        message = f"{method} {target} could not reach the API: {reason}."
        hint = ("Start the API (python3 -m orchestration.cli serve) and confirm AI_WEBSITE_AGENT_API_URL "
                "names that port; a refused connection means nothing was executed.")
    else:
        kind = "invalid_response"
        message = f"{method} {target} returned a response this verifier could not read: {error}."
        hint = "The API must answer with a JSON object; a proxy or error page in front of it is a configuration problem."
    detail = {"error": message, "error_kind": kind, "status": "FAILED", "method": method, "url": target}
    if hint:
        detail["next_action"] = hint
    return detail


def validate_n8n_workflow_json(workflow_path: str | Path) -> dict[str, Any]:
    """Validate that an exported n8n workflow file has valid nodes, connections, and no embedded secrets."""
    path = Path(workflow_path)
    if not path.is_file():
        raise ValueError(
            f"n8n workflow file not found: {path}. Export it from n8n into n8n/workflows/ or pass the "
            "path of the downloaded JSON; the repository copies are the importable starting point."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"n8n workflow {path} is not valid JSON: {error.msg} at line {error.lineno}.") from error
    if not isinstance(data, dict) or not data.get("name") or not isinstance(data.get("nodes"), list):
        raise ValueError(f"Invalid n8n workflow file: {path}")
    if data.get("active") is not False:
        raise ValueError(f"Workflow {path} must have 'active': false in repository exports.")
    node_names = {node["name"] for node in data["nodes"] if isinstance(node, dict) and node.get("name")}
    if len(node_names) != len(data["nodes"]):
        raise ValueError(f"Duplicate or missing node names in workflow: {path}")
    connections = data.get("connections") or {}
    for src_name, conn_map in connections.items():
        if src_name not in node_names:
            raise ValueError(f"Connection source '{src_name}' does not match any node in {path}")
        for branch in conn_map.get("main") or []:
            for edge in branch:
                target_name = edge.get("node")
                if target_name not in node_names:
                    raise ValueError(f"Connection target '{target_name}' does not match any node in {path}")
    raw_text = path.read_text(encoding="utf-8")
    if re.search(r"Bearer\s+(?!\{\{\s*\$env\.AI_WEBSITE_AGENT_API_TOKEN\s*\}\})[A-Za-z0-9._-]+", raw_text):
        raise ValueError(f"Workflow {path} contains a hardcoded Bearer token instead of $env.AI_WEBSITE_AGENT_API_TOKEN.")
    return data


def _lookup_path(obj: Any, dotted_path: str) -> Any:
    cur = obj
    for part in dotted_path.split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _eval_atom(
    atom: str,
    *,
    current_json: dict[str, Any],
    node_outputs: dict[str, dict[str, Any]],
    env: dict[str, str],
    execution_id: str,
) -> Any:
    token = atom.strip()
    if token.startswith("(") and token.endswith(")"):
        token = token[1:-1].strip()
    if "+" in token and not (token.startswith("'") and token.endswith("'")) and not (token.startswith('"') and token.endswith('"')):
        parts = [p.strip() for p in token.split("+")]
        evaluated = [
            _eval_atom(p, current_json=current_json, node_outputs=node_outputs, env=env, execution_id=execution_id)
            for p in parts
        ]
        return "".join("" if v is None else str(v) for v in evaluated)
    if (token.startswith("'") and token.endswith("'")) or (token.startswith('"') and token.endswith('"')):
        return token[1:-1]
    if token == "true":
        return True
    if token == "false":
        return False
    if token == "null":
        return None
    if re.fullmatch(r"-?\d+", token):
        return int(token)
    if token == "$execution.id":
        return execution_id
    if token.startswith("$env."):
        return env.get(token[len("$env."):], "")
    if token.startswith("$json."):
        return _lookup_path(current_json, token[len("$json."):])
    node_match = re.fullmatch(r"\$node\[['\"]([^'\"]+)['\"]\]\.json\.(.+)", token)
    if node_match:
        ref_node, ref_path = node_match.group(1), node_match.group(2)
        return _lookup_path(node_outputs.get(ref_node, {}), ref_path)
    return token


def _eval_expr_content(
    expr: str,
    *,
    current_json: dict[str, Any],
    node_outputs: dict[str, dict[str, Any]],
    env: dict[str, str],
    execution_id: str,
) -> Any:
    clauses = [c.strip() for c in expr.split("||")]
    for clause in clauses:
        val = _eval_atom(
            clause,
            current_json=current_json,
            node_outputs=node_outputs,
            env=env,
            execution_id=execution_id,
        )
        if val not in (None, ""):
            return val
    return _eval_atom(
        clauses[-1],
        current_json=current_json,
        node_outputs=node_outputs,
        env=env,
        execution_id=execution_id,
    )


def evaluate_n8n_template(
    raw: Any,
    *,
    current_json: dict[str, Any],
    node_outputs: dict[str, dict[str, Any]],
    env: dict[str, str],
    execution_id: str,
    json_context: bool = False,
) -> Any:
    if not isinstance(raw, str):
        return raw
    text = raw[1:] if raw.startswith("=") else raw
    whole = re.fullmatch(r"\{\{\s*((?:(?!\}\}).)+?)\s*\}\}", text.strip(), flags=re.DOTALL)
    if whole:
        return _eval_expr_content(
            whole.group(1),
            current_json=current_json,
            node_outputs=node_outputs,
            env=env,
            execution_id=execution_id,
        )

    def _is_inside_json_string(position: int) -> bool:
        in_string = False
        escaped = False
        for char in text[:position]:
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
            elif char == '"':
                in_string = not in_string
        return in_string

    def _replace(match: re.Match[str]) -> str:
        val = _eval_expr_content(
            match.group(1),
            current_json=current_json,
            node_outputs=node_outputs,
            env=env,
            execution_id=execution_id,
        )

        if json_context:
            if _is_inside_json_string(match.start()):
                if val is None:
                    return ""
                if isinstance(val, bool):
                    val_text = "true" if val else "false"
                elif isinstance(val, (dict, list)):
                    val_text = json.dumps(val, ensure_ascii=False)
                else:
                    val_text = str(val)
                # Escape quotes, backslashes and control characters as JSON string content.
                return json.dumps(val_text, ensure_ascii=False)[1:-1]

            if val is None:
                return "null"

            # Some workflow fields intentionally contain raw JSON text, such as retry input.
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except json.JSONDecodeError:
                    pass

            return json.dumps(val, ensure_ascii=False)

        # Preserve the original behavior for URLs, headers and non-JSON templates.
        if isinstance(val, bool):
            return "true" if val else "false"
        if val is None:
            return ""
        return str(val)

    return re.sub(r"\{\{\s*((?:(?!\}\}).)+?)\s*\}\}", _replace, text, flags=re.DOTALL)


class N8nWorkflowRunner:
    """Executes an exported n8n workflow JSON graph against the local HTTP API."""

    def __init__(
        self,
        workflow_path: str | Path,
        *,
        env: dict[str, str],
        node_hooks: dict[str, Callable[[dict[str, Any]], None]] | None = None,
        allow_remote: bool = False,
        timeout_seconds: int = N8N_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.workflow_path = Path(workflow_path)
        self.workflow = validate_n8n_workflow_json(self.workflow_path)
        self.nodes_by_name = {node["name"]: node for node in self.workflow["nodes"]}
        self.connections = self.workflow.get("connections") or {}
        self.env = dict(env)
        self.node_hooks = dict(node_hooks or {})
        # An operator may opt into a remote target explicitly; the default refuses it because each
        # request carries the API bearer token.
        self.allow_remote = bool(allow_remote)
        self.timeout_seconds = max(1, int(timeout_seconds))

    def execute(
        self,
        trigger_data: dict[str, Any] | None = None,
        *,
        execution_id: str = "exec-001",
    ) -> dict[str, Any]:
        node_outputs: dict[str, dict[str, Any]] = {}
        trace: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        current_node_name = self._find_trigger_node()
        current_json: dict[str, Any] = dict(trigger_data or {})

        while current_node_name:
            node = self.nodes_by_name[current_node_name]
            ntype = node["type"]
            params = node.get("parameters") or {}
            branch_index = 0

            if ntype == "n8n-nodes-base.manualTrigger":
                out_json = dict(current_json)
            elif ntype == "n8n-nodes-base.set":
                out_json = self._run_set_node(params, current_json, node_outputs, execution_id)
            elif ntype == "n8n-nodes-base.httpRequest":
                out_json = self._run_http_node(params, current_json, node_outputs, execution_id)
            elif ntype == "n8n-nodes-base.if":
                matched = self._run_if_node(params, current_json, node_outputs, execution_id)
                branch_index = 0 if matched else 1
                out_json = dict(current_json)
            else:
                raise ValueError(f"Unsupported n8n node type in runner: {ntype}")

            node_outputs[current_node_name] = out_json
            trace.append({
                "node": current_node_name,
                "type": ntype,
                "branch_index": branch_index if ntype == "n8n-nodes-base.if" else None,
                "output": out_json,
            })
            if isinstance(out_json, dict) and out_json.get("error_kind"):
                errors.append({"node": current_node_name, **{
                    key: out_json.get(key)
                    for key in ("error", "error_kind", "error_code", "http_status", "method", "url")
                    if out_json.get(key) is not None
                }})
            if current_node_name in self.node_hooks:
                self.node_hooks[current_node_name](out_json)

            current_json = out_json
            next_branches = (self.connections.get(current_node_name) or {}).get("main") or []
            if branch_index < len(next_branches) and next_branches[branch_index]:
                current_node_name = next_branches[branch_index][0]["node"]
            else:
                current_node_name = None

        return {
            "workflow_name": self.workflow["name"],
            "execution_id": execution_id,
            "visited_nodes": [step["node"] for step in trace],
            "terminal_node": trace[-1]["node"] if trace else None,
            # A failed node is reported, not raised: the workflow's own outcome nodes still decide what
            # an operator sees, and `execution_status` keeps "the run completed" distinct from
            # "every step succeeded".
            "execution_status": "FAILED" if errors else "COMPLETED",
            "errors": errors,
            "final_output": current_json,
            "node_outputs": node_outputs,
            "trace": trace,
        }

    def _find_trigger_node(self) -> str:
        for node in self.workflow["nodes"]:
            if node["type"] == "n8n-nodes-base.manualTrigger":
                return node["name"]
        raise ValueError(f"No Manual Trigger node found in {self.workflow_path}")

    def _run_set_node(
        self,
        params: dict[str, Any],
        current_json: dict[str, Any],
        node_outputs: dict[str, dict[str, Any]],
        execution_id: str,
    ) -> dict[str, Any]:
        values = params.get("values") or {}
        result: dict[str, Any] = {}
        for item in values.get("string") or []:
            val = evaluate_n8n_template(
                item.get("value"),
                current_json=current_json,
                node_outputs=node_outputs,
                env=self.env,
                execution_id=execution_id,
            )
            result[item["name"]] = "" if val is None else str(val)
        for item in values.get("number") or []:
            val = evaluate_n8n_template(
                item.get("value"),
                current_json=current_json,
                node_outputs=node_outputs,
                env=self.env,
                execution_id=execution_id,
            )
            result[item["name"]] = int(val) if val not in (None, "") else 0
        for item in values.get("boolean") or []:
            val = evaluate_n8n_template(
                item.get("value"),
                current_json=current_json,
                node_outputs=node_outputs,
                env=self.env,
                execution_id=execution_id,
            )
            if isinstance(val, str):
                result[item["name"]] = val.strip().lower() == "true"
            else:
                result[item["name"]] = bool(val)
        return result

    def _run_http_node(
        self,
        params: dict[str, Any],
        current_json: dict[str, Any],
        node_outputs: dict[str, dict[str, Any]],
        execution_id: str,
    ) -> dict[str, Any]:
        method = str(params.get("method") or "GET").upper()
        url = str(
            evaluate_n8n_template(
                params.get("url"),
                current_json=current_json,
                node_outputs=node_outputs,
                env=self.env,
                execution_id=execution_id,
            )
        )
        headers: dict[str, str] = {}
        for h_item in ((params.get("headerParameters") or {}).get("parameters") or []):
            h_val = evaluate_n8n_template(
                h_item.get("value"),
                current_json=current_json,
                node_outputs=node_outputs,
                env=self.env,
                execution_id=execution_id,
            )
            headers[h_item["name"]] = str(h_val)
        body_bytes: bytes | None = None
        if params.get("sendBody") and method in {"POST", "PUT", "PATCH"}:
            rendered_body = evaluate_n8n_template(
                params.get("jsonBody") or "{}",
                current_json=current_json,
                node_outputs=node_outputs,
                env=self.env,
                execution_id=execution_id,
                json_context=True,
            )
            if isinstance(rendered_body, (dict, list, int, float, bool)) or rendered_body is None:
                parsed_body = rendered_body
            else:
                parsed_body = json.loads(str(rendered_body))
            body_bytes = json.dumps(parsed_body).encode("utf-8")
        # Policy first: the host check happens before the request is built, so a refused target never
        # sees a header set containing the API credential.
        _assert_loopback_target(url, allow_remote=self.allow_remote)
        req = Request(url, data=body_bytes, headers=headers, method=method)
        try:
            with urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read()
        except (HTTPError, URLError, socket.timeout, TimeoutError, OSError) as error:
            detail = _describe_failure(error, method=method, url=url, timeout_seconds=self.timeout_seconds)
            if isinstance(error, HTTPError):
                detail["http_status"] = error.code
                body_text = error.read().decode("utf-8", errors="replace")
                try:
                    parsed_err = json.loads(body_text)
                except json.JSONDecodeError:
                    parsed_err = {"error": body_text.strip() or detail["error"]}
                if isinstance(parsed_err, dict):
                    # Keep the API's own explanation (error, error_code, detail) and add the transport facts.
                    parsed_err.setdefault("error", detail["error"])
                    detail = {**detail, **parsed_err}
                    detail["error_kind"] = "http_error"
            return detail
        if not raw.strip():
            detail = {
                "error": f"{method} {urlsplit(url).netloc}{urlsplit(url).path} answered with an empty body.",
                "error_kind": "empty_response", "status": "FAILED", "method": method,
            }
            return detail
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            # A 2xx that is not readable JSON is a failed answer, not a success with odd formatting.
            detail = _describe_failure(error, method=method, url=url, timeout_seconds=self.timeout_seconds)
            detail["http_status"] = 200
            return detail
        if not isinstance(parsed, dict):
            return {"error": "API response was not a JSON object.", "error_kind": "invalid_response",
                    "status": "FAILED", "response": parsed}
        return parsed

    def _run_if_node(
        self,
        params: dict[str, Any],
        current_json: dict[str, Any],
        node_outputs: dict[str, dict[str, Any]],
        execution_id: str,
    ) -> bool:
        conds = (params.get("conditions") or {}).get("string") or []
        combine = str(params.get("combineOperation") or "all").lower()
        results: list[bool] = []
        for cond in conds:
            v1 = evaluate_n8n_template(
                cond.get("value1"),
                current_json=current_json,
                node_outputs=node_outputs,
                env=self.env,
                execution_id=execution_id,
            )
            op = cond.get("operation") or "equal"
            v2 = evaluate_n8n_template(
                cond.get("value2"),
                current_json=current_json,
                node_outputs=node_outputs,
                env=self.env,
                execution_id=execution_id,
            )
            s1 = "" if v1 is None else str(v1)
            s2 = "" if v2 is None else str(v2)
            if op == "equal":
                results.append(s1 == s2)
            elif op == "notEqual":
                results.append(s1 != s2)
            elif op == "isNotEmpty":
                results.append(bool(s1.strip()))
            elif op == "isEmpty":
                results.append(not bool(s1.strip()))
            else:
                raise ValueError(f"Unsupported IF operation: {op}")
        return any(results) if combine == "any" else all(results)

"""Local verifier and graph runner for exported n8n workflow JSON files.

Because the sandbox environment does not have a live n8n binary/Node.js runtime
installed, this runner parses the actual exported n8n workflow JSON definitions
(`nodes`, `connections`, `parameters`, and `={{ ... }}` expressions) and
executes their HTTP requests over loopback (`127.0.0.1`) against the running
`orchestration.api` server.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def validate_n8n_workflow_json(workflow_path: str | Path) -> dict[str, Any]:
    """Validate that an exported n8n workflow file has valid nodes, connections, and no embedded secrets."""
    path = Path(workflow_path)
    data = json.loads(path.read_text(encoding="utf-8"))
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

    def _replace(match: re.Match[str]) -> str:
        val = _eval_expr_content(
            match.group(1),
            current_json=current_json,
            node_outputs=node_outputs,
            env=env,
            execution_id=execution_id,
        )
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
    ) -> None:
        self.workflow_path = Path(workflow_path)
        self.workflow = validate_n8n_workflow_json(self.workflow_path)
        self.nodes_by_name = {node["name"]: node for node in self.workflow["nodes"]}
        self.connections = self.workflow.get("connections") or {}
        self.env = dict(env)
        self.node_hooks = dict(node_hooks or {})

    def execute(
        self,
        trigger_data: dict[str, Any] | None = None,
        *,
        execution_id: str = "exec-001",
    ) -> dict[str, Any]:
        node_outputs: dict[str, dict[str, Any]] = {}
        trace: list[dict[str, Any]] = []
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
            )
            parsed_body = json.loads(str(rendered_body))
            body_bytes = json.dumps(parsed_body).encode("utf-8")
        req = Request(url, data=body_bytes, headers=headers, method=method)
        try:
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as err:
            err_body = err.read().decode("utf-8", errors="replace")
            try:
                parsed_err = json.loads(err_body)
            except json.JSONDecodeError:
                parsed_err = {"error": err_body}
            parsed_err["http_status"] = err.code
            return parsed_err

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

"""
backend/agent.py
────────────────────────────────────────
The AI brain. Connects Groq / Claude / Gemini to the MCP Server.
Manages the conversation loop, tool calling, and pending
approval state for write operations.

FIXES APPLIED:
1. MCP connection uses AsyncExitStack (proper persistent connection)
2. All Groq/Claude API calls wrapped in run_in_executor (non-blocking)
3. Tool call loop adds tool result to messages BEFORE returning approval
4. Gemini rewritten to use google-genai SDK (current API)
5. _mcp_connected flag added for health checks
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from contextlib import AsyncExitStack
from typing import Any, Optional

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import (
    GROQ_API_KEY,
    CLAUDE_API_KEY,
    GEMINI_API_KEY,
    AI_PROVIDER,
    GROQ_MODEL,
    CLAUDE_MODEL,
    GEMINI_MODEL,
    MCP_SERVER_URL,
    PENDING_ACTION_TIMEOUT_SECONDS,
)
from server.guardrails import is_write_operation
from server.audit_log import log_approval_given, log_approval_denied

logger = logging.getLogger("kubeguardian.agent")

# ──────────────────────────────────────────────
# System prompt
# ──────────────────────────────────────────────

SYSTEM_PROMPT = """You are **EKS Guardian**, an expert Kubernetes cluster management assistant.
You help DevOps engineers manage and monitor their Amazon EKS clusters safely and efficiently.

## Your Identity
- You are precise, safety-conscious, and always explain what you are doing and why.
- You format responses clearly with bullet points, status emojis (✅ ❌ ⚠️), bold text, and code blocks.
- You use tables for structured data (pod lists, node status).
- You always provide context — never just raw data without explanation.

## Safety Rules (YOU CANNOT OVERRIDE THESE)
- You NEVER execute `kubectl drain` — you only generate assessment reports and commands.
- You NEVER scale deployments to 0 replicas.
- You NEVER scale above 20 replicas.
- You NEVER modify kube-system, kube-public, or kube-node-lease namespaces.
- You NEVER patch anything other than labels and annotations.
- You NEVER delete any Kubernetes resource.
- You NEVER expose Kubernetes secrets, tokens, or credential values.
- All write operations go through an approval gate — the engineer must approve before execution.

## Response Formatting
- Use **bold** for important values and resource names.
- Use `code` for kubectl commands, resource names inline.
- Use ```code blocks``` for multi-line commands.
- Use ✅ for healthy/running, ❌ for failed/error, ⚠️ for warnings/pending.
- Use tables (markdown) for listing pods, nodes, deployments.
- Use numbered lists for step-by-step procedures.
- Keep responses concise but thorough.

## Behavior
- When asked about cluster health, always provide a structured overview.
- When asked about draining a node, ALWAYS use the assess_node_maintenance tool.
- When asked to scale/restart/patch, explain the current state first, then propose the change.
- For follow-up questions, use conversation context to understand references like "that deployment" or "scale it up".
"""

# ──────────────────────────────────────────────
# Execute tool name mapping
# ──────────────────────────────────────────────

EXECUTE_TOOL_MAP = {
    "scale_deployment":    "execute_scale_deployment",
    "restart_deployment":  "execute_restart_deployment",
    "patch_resource_labels": "execute_patch_resource",
    "cordon_node":         "execute_cordon_node",
}


# ──────────────────────────────────────────────
# Helper: build approval payload from pre-check result
# ──────────────────────────────────────────────

def _build_action_plan(tool_name: str, tool_args: dict, result_data: dict) -> dict:
    """
    Build the action_plan dict that gets sent to the frontend
    approval card. Centralised here so all three providers
    produce identical output.
    """
    target = (
        result_data.get("deployment")
        or result_data.get("resource_name")
        or result_data.get("node_name")
        or tool_args.get("deployment_name")
        or tool_args.get("resource_name")
        or tool_args.get("node_name")
        or "Unknown"
    )
    current_state = (
        f"{result_data['current_replicas']} replicas"
        if "current_replicas" in result_data
        else str(result_data.get("current_unschedulable", ""))
    )
    return {
        "operation":       result_data.get("operation", tool_name),
        "target":          target,
        "namespace":       result_data.get("namespace", ""),
        "current_state":   current_state,
        "proposed_change": result_data.get("action_plan", ""),
        "risk_level":      result_data.get("risk_level", "LOW"),
        "impact":          result_data.get("impact", ""),
        "pre_check_results": result_data.get("risks", []),
    }


# ══════════════════════════════════════════════
# MCPGroqAgent
# ══════════════════════════════════════════════

class MCPGroqAgent:
    """
    AI Agent that bridges Groq / Claude / Gemini with the
    MCP Server for Kubernetes management.
    """

    def __init__(self):
        self.provider = AI_PROVIDER
        self._ai_client = None
        self._model: str = ""
        self._init_ai_client()

        # MCP — managed by AsyncExitStack for clean lifecycle
        self._exit_stack = AsyncExitStack()
        self._mcp_session: Optional[ClientSession] = None
        self._mcp_connected: bool = False

        # Conversation history (last 10 exchanges = 20 messages)
        self.conversation_history: list[dict] = []
        self.max_history = 10

        # Pending write actions awaiting user approval
        self.pending_actions: dict[str, dict] = {}

        # Tool definitions fetched from MCP, converted for the AI
        self._tools_cache: list[dict] = []

    # ────────────── AI client init ──────────────

    def _init_ai_client(self) -> None:
        """Initialise the correct AI SDK based on AI_PROVIDER."""
        if self.provider == "groq":
            from groq import Groq
            self._ai_client = Groq(api_key=GROQ_API_KEY)
            self._model = GROQ_MODEL
            logger.info("AI Provider: Groq  model=%s", self._model)

        elif self.provider == "claude":
            import anthropic
            self._ai_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
            self._model = CLAUDE_MODEL
            logger.info("AI Provider: Claude  model=%s", self._model)

        elif self.provider == "gemini":
            # google-genai ≥ 1.0 (pip install google-genai)
            from google import genai as google_genai
            self._ai_client = google_genai.Client(api_key=GEMINI_API_KEY)
            self._model = GEMINI_MODEL
            logger.info("AI Provider: Gemini  model=%s", self._model)

        else:
            raise ValueError(f"Unknown AI_PROVIDER: '{self.provider}'. "
                             "Must be 'groq', 'claude', or 'gemini'.")

    # ────────────── MCP connection ──────────────

    async def connect_mcp(self) -> None:
        """
        Open a persistent connection to the MCP Server.

        Uses AsyncExitStack so both the streamable-HTTP transport
        and the ClientSession context managers stay alive for the
        entire lifetime of the FastAPI application.
        """
        logger.info("Connecting to MCP Server at %s", MCP_SERVER_URL)
        try:
            # Enter the HTTP transport — yields (read, write, _)
            read_stream, write_stream, _ = (
                await self._exit_stack.enter_async_context(
                    streamablehttp_client(MCP_SERVER_URL)
                )
            )
            # Enter the MCP session
            self._mcp_session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await self._mcp_session.initialize()
            self._mcp_connected = True
            logger.info("MCP Server connected successfully")

            # Pull tool definitions and convert for the AI
            await self._refresh_tools()

        except Exception as exc:
            self._mcp_connected = False
            logger.error("MCP connection failed: %s", exc)
            raise

    async def disconnect_mcp(self) -> None:
        """Close MCP session and HTTP transport cleanly."""
        try:
            await self._exit_stack.aclose()
            self._mcp_connected = False
            logger.info("MCP Server disconnected")
        except Exception as exc:
            logger.warning("Error during MCP disconnect: %s", exc)

    # ────────────── Tool management ──────────────

    async def _refresh_tools(self) -> None:
        """Fetch tool list from MCP and build AI-compatible specs."""
        tools_result = await self._mcp_session.list_tools()
        self._tools_cache = self._convert_tools_for_ai(tools_result.tools)
        logger.info("Loaded %d tools from MCP", len(self._tools_cache))

    def _convert_tools_for_ai(self, mcp_tools) -> list[dict]:
        """
        Convert MCP Tool objects → OpenAI function-calling schema.
        (Groq and Claude both accept this format; Gemini is converted
        separately in _process_gemini.)

        execute_* tools are hidden from the AI — they are called
        internally only after the user approves.
        """
        tools: list[dict] = []
        for tool in mcp_tools:
            if tool.name.startswith("execute_"):
                continue

            properties: dict = {}
            required: list[str] = []

            if tool.inputSchema and "properties" in tool.inputSchema:
                for param_name, schema in tool.inputSchema["properties"].items():
                    properties[param_name] = {
                        "type": schema.get("type", "string"),
                        "description": schema.get("description", ""),
                    }
                required = tool.inputSchema.get("required", [])

            tools.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            })
        return tools

    # ────────────── Conversation history ──────────────

    def _add_to_history(self, role: str, content: str) -> None:
        """Append a message; trim to last max_history exchanges."""
        self.conversation_history.append({"role": role, "content": content})
        limit = self.max_history * 2
        if len(self.conversation_history) > limit:
            self.conversation_history = self.conversation_history[-limit:]

    # ────────────── MCP tool helper ──────────────

    async def _call_mcp_tool(self, tool_name: str, tool_args: dict) -> str:
        """
        Call an MCP tool and return the raw text result.
        Centralised so all three provider paths use the same logic.
        """
        result = await self._mcp_session.call_tool(tool_name, tool_args)
        text = ""
        for item in result.content:
            if hasattr(item, "text"):
                text += item.text
        return text

    # ────────────── Main entry point ──────────────

    async def process_message(self, user_message: str) -> dict:
        """
        Route a user message through the correct AI provider.

        Returns:
            {"type": "response", "message": str}
            OR
            {"type": "APPROVAL_REQUIRED", "action_id": str, "action_plan": dict, ...}
        """
        self._add_to_history("user", user_message)
        self.cleanup_expired_actions()

        try:
            if self.provider == "groq":
                return await self._process_groq()
            elif self.provider == "claude":
                return await self._process_claude()
            elif self.provider == "gemini":
                return await self._process_gemini(user_message)
        except Exception as exc:
            logger.error("process_message failed: %s", exc, exc_info=True)
            msg = f"❌ **Error processing your request:** {exc}"
            self._add_to_history("assistant", msg)
            return {"type": "response", "message": msg}

    # ══════════════════════════════════════════════
    # GROQ provider
    # ══════════════════════════════════════════════

    async def _process_groq(self) -> dict:
        """
        Full Groq agentic loop.

        Groq's SDK is synchronous, so every API call is wrapped in
        asyncio.get_event_loop().run_in_executor() to avoid blocking
        FastAPI's event loop.
        """
        loop = asyncio.get_event_loop()

        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *self.conversation_history,
        ]

        # ── First Groq call ──
        response = await loop.run_in_executor(
            None,
            lambda: self._ai_client.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=self._tools_cache or None,
                tool_choice="auto",
                max_tokens=4096,
            ),
        )
        response_message = response.choices[0].message

        # No tool calls — plain text answer
        if not response_message.tool_calls:
            content = response_message.content or ""
            self._add_to_history("assistant", content)
            return {"type": "response", "message": content}

        # ── Tool call loop ──
        # Add assistant's tool-call message to the thread
        messages.append({
            "role": "assistant",
            "content": response_message.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in response_message.tool_calls
            ],
        })

        for tool_call in response_message.tool_calls:
            tool_name = tool_call.function.name
            tool_args = json.loads(tool_call.function.arguments)
            logger.info("Groq tool call: %s  args=%s", tool_name, tool_args)

            result_text = await self._call_mcp_tool(tool_name, tool_args)

            # ── Always add tool result to messages first ──
            # This is critical: Groq requires every tool_call_id
            # to have a matching tool result in the thread.
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result_text,
            })

            if is_write_operation(tool_name):
                try:
                    result_data = json.loads(result_text)
                except json.JSONDecodeError:
                    result_data = {}

                # Guardrail blocked the operation
                if result_data.get("blocked"):
                    final = await loop.run_in_executor(
                        None,
                        lambda: self._ai_client.chat.completions.create(
                            model=self._model,
                            messages=messages,
                            max_tokens=2048,
                        ),
                    )
                    msg = final.choices[0].message.content or ""
                    self._add_to_history("assistant", msg)
                    return {"type": "response", "message": msg}

                # Pre-check passed — needs user approval
                if result_data.get("pre_check"):
                    action_id = str(uuid.uuid4())
                    self.pending_actions[action_id] = {
                        "tool_name":        tool_name,
                        "tool_args":        tool_args,
                        "pre_check_result": result_data,
                        "timestamp":        time.time(),
                    }
                    return {
                        "type":        "APPROVAL_REQUIRED",
                        "action_id":   action_id,
                        "tool_name":   tool_name,
                        "tool_args":   tool_args,
                        "action_plan": _build_action_plan(tool_name, tool_args, result_data),
                    }

        # ── Final Groq call with all tool results ──
        final_response = await loop.run_in_executor(
            None,
            lambda: self._ai_client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_tokens=4096,
            ),
        )
        content = final_response.choices[0].message.content or ""
        self._add_to_history("assistant", content)
        return {"type": "response", "message": content}

    # ══════════════════════════════════════════════
    # CLAUDE provider
    # ══════════════════════════════════════════════

    async def _process_claude(self) -> dict:
        """
        Full Claude agentic loop.

        Claude's SDK is also synchronous — same run_in_executor
        pattern used here.
        """
        loop = asyncio.get_event_loop()

        # Convert to Claude tool format
        claude_tools = [
            {
                "name":         t["function"]["name"],
                "description":  t["function"]["description"],
                "input_schema": t["function"]["parameters"],
            }
            for t in self._tools_cache
        ]

        # Build message list (exclude system — passed separately)
        messages: list[dict] = [
            {"role": m["role"], "content": m["content"]}
            for m in self.conversation_history
        ]

        # ── First Claude call ──
        response = await loop.run_in_executor(
            None,
            lambda: self._ai_client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=claude_tools,
                messages=messages,
            ),
        )

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        # No tool calls
        if not tool_use_blocks:
            text = "".join(b.text for b in response.content if b.type == "text")
            self._add_to_history("assistant", text)
            return {"type": "response", "message": text}

        # ── Process tool calls ──
        # Add full assistant response (may contain text + tool_use blocks)
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []

        for tool_block in tool_use_blocks:
            tool_name = tool_block.name
            tool_args = tool_block.input
            logger.info("Claude tool call: %s  args=%s", tool_name, tool_args)

            result_text = await self._call_mcp_tool(tool_name, tool_args)

            # Collect tool result (Claude batches them in one user turn)
            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": tool_block.id,
                "content":     result_text,
            })

            if is_write_operation(tool_name):
                try:
                    result_data = json.loads(result_text)
                except json.JSONDecodeError:
                    result_data = {}

                if result_data.get("blocked"):
                    msg = (
                        f"⚠️ **Operation Blocked by Safety Guardrail**\n\n"
                        f"{result_data.get('reason', 'Unknown reason')}"
                    )
                    self._add_to_history("assistant", msg)
                    return {"type": "response", "message": msg}

                if result_data.get("pre_check"):
                    action_id = str(uuid.uuid4())
                    self.pending_actions[action_id] = {
                        "tool_name":        tool_name,
                        "tool_args":        tool_args,
                        "pre_check_result": result_data,
                        "timestamp":        time.time(),
                    }
                    return {
                        "type":        "APPROVAL_REQUIRED",
                        "action_id":   action_id,
                        "tool_name":   tool_name,
                        "tool_args":   tool_args,
                        "action_plan": _build_action_plan(tool_name, tool_args, result_data),
                    }

        # Add all tool results as a single user turn (Claude's requirement)
        messages.append({"role": "user", "content": tool_results})

        # ── Final Claude call ──
        final = await loop.run_in_executor(
            None,
            lambda: self._ai_client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=claude_tools,
                messages=messages,
            ),
        )
        text = "".join(b.text for b in final.content if b.type == "text")
        self._add_to_history("assistant", text)
        return {"type": "response", "message": text}

    # ══════════════════════════════════════════════
    # GEMINI provider  (google-genai ≥ 1.0)
    # ══════════════════════════════════════════════

    async def _process_gemini(self, user_message: str) -> dict:
        """
        Full Gemini agentic loop using the google-genai SDK (≥ 1.0).

        This SDK has a proper async client, so no run_in_executor needed.
        """
        from google.genai import types as genai_types

        # Build tool declarations
        tool_declarations = []
        for tool in self._tools_cache:
            params = tool["function"]["parameters"]
            properties = {}
            for pname, pschema in params.get("properties", {}).items():
                raw_type = pschema.get("type", "string").upper()
                # Map JSON Schema types to Gemini Schema types
                type_map = {
                    "STRING":  "STRING",
                    "INTEGER": "INTEGER",
                    "NUMBER":  "NUMBER",
                    "BOOLEAN": "BOOLEAN",
                    "OBJECT":  "OBJECT",
                    "ARRAY":   "ARRAY",
                }
                gemini_type = type_map.get(raw_type, "STRING")
                properties[pname] = genai_types.Schema(
                    type=gemini_type,
                    description=pschema.get("description", ""),
                )

            tool_declarations.append(
                genai_types.FunctionDeclaration(
                    name=tool["function"]["name"],
                    description=tool["function"]["description"],
                    parameters=genai_types.Schema(
                        type="OBJECT",
                        properties=properties,
                        required=params.get("required", []),
                    ),
                )
            )

        gemini_tool = genai_types.Tool(function_declarations=tool_declarations)
        config = genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[gemini_tool],
        )

        # Build conversation history for Gemini
        contents: list[genai_types.Content] = []
        for msg in self.conversation_history:
            role = "user" if msg["role"] == "user" else "model"
            contents.append(
                genai_types.Content(
                    role=role,
                    parts=[genai_types.Part(text=msg["content"])],
                )
            )

        # ── First Gemini call (async) ──
        response = await self._ai_client.aio.models.generate_content(
            model=self._model,
            contents=contents,
            config=config,
        )

        # ── Tool call loop ──
        # Gemini may request multiple rounds of function calls
        max_rounds = 5
        for _ in range(max_rounds):
            # Find function call parts
            fc_parts = [
                p for p in (response.candidates[0].content.parts or [])
                if p.function_call is not None
            ]

            if not fc_parts:
                break  # No more tool calls — get final text

            # Add model's response to contents
            contents.append(response.candidates[0].content)

            # Execute each function call and collect responses
            function_responses = []
            approval_needed = None

            for part in fc_parts:
                fc = part.function_call
                tool_name = fc.name
                tool_args = dict(fc.args) if fc.args else {}
                logger.info("Gemini tool call: %s  args=%s", tool_name, tool_args)

                result_text = await self._call_mcp_tool(tool_name, tool_args)

                # Always collect the function response
                function_responses.append(
                    genai_types.Part(
                        function_response=genai_types.FunctionResponse(
                            name=tool_name,
                            response={"result": result_text},
                        )
                    )
                )

                if is_write_operation(tool_name):
                    try:
                        result_data = json.loads(result_text)
                    except json.JSONDecodeError:
                        result_data = {}

                    if result_data.get("blocked"):
                        msg = (
                            f"⚠️ **Operation Blocked by Safety Guardrail**\n\n"
                            f"{result_data.get('reason', 'Unknown reason')}"
                        )
                        self._add_to_history("assistant", msg)
                        return {"type": "response", "message": msg}

                    if result_data.get("pre_check"):
                        # Store approval info — we still send function
                        # responses to Gemini but will return approval UI
                        action_id = str(uuid.uuid4())
                        self.pending_actions[action_id] = {
                            "tool_name":        tool_name,
                            "tool_args":        tool_args,
                            "pre_check_result": result_data,
                            "timestamp":        time.time(),
                        }
                        approval_needed = {
                            "type":        "APPROVAL_REQUIRED",
                            "action_id":   action_id,
                            "tool_name":   tool_name,
                            "tool_args":   tool_args,
                            "action_plan": _build_action_plan(
                                tool_name, tool_args, result_data
                            ),
                        }

            # Add all function responses as a user turn
            contents.append(
                genai_types.Content(
                    role="user",
                    parts=function_responses,
                )
            )

            # Return approval gate before next Gemini call if needed
            if approval_needed:
                return approval_needed

            # Continue the loop with next Gemini call
            response = await self._ai_client.aio.models.generate_content(
                model=self._model,
                contents=contents,
                config=config,
            )

        # ── Extract final text ──
        text = ""
        for part in (response.candidates[0].content.parts or []):
            if hasattr(part, "text") and part.text:
                text += part.text

        if not text:
            text = "I completed the requested operations. Let me know if you need anything else."

        self._add_to_history("assistant", text)
        return {"type": "response", "message": text}

    # ══════════════════════════════════════════════
    # Approval flow
    # ══════════════════════════════════════════════

    async def execute_approved_action(self, action_id: str) -> dict:
        """
        Execute a write operation after the user clicked Approve.

        Looks up the pending action by UUID, calls the matching
        execute_* MCP tool, logs the outcome, and returns a
        human-readable result.
        """
        if action_id not in self.pending_actions:
            return {
                "type": "response",
                "message": (
                    "❌ **Action not found.** "
                    "It may have expired or already been processed."
                ),
            }

        action = self.pending_actions[action_id]

        # Timeout guard
        if time.time() - action["timestamp"] > PENDING_ACTION_TIMEOUT_SECONDS:
            del self.pending_actions[action_id]
            return {
                "type": "response",
                "message": (
                    f"❌ **Action expired.** "
                    f"The {PENDING_ACTION_TIMEOUT_SECONDS}s approval window has passed. "
                    "Please request the operation again."
                ),
            }

        tool_name    = action["tool_name"]
        tool_args    = action["tool_args"]
        execute_tool = EXECUTE_TOOL_MAP.get(tool_name)

        if not execute_tool:
            del self.pending_actions[action_id]
            return {
                "type": "response",
                "message": f"❌ **No execute handler found for '{tool_name}'.**",
            }

        try:
            # Derive target / namespace for audit log
            target = (
                tool_args.get("deployment_name")
                or tool_args.get("resource_name")
                or tool_args.get("node_name")
                or "Unknown"
            )
            namespace = tool_args.get("namespace", "cluster")
            log_approval_given(tool_name, target, namespace)

            result_text = await self._call_mcp_tool(execute_tool, tool_args)
            del self.pending_actions[action_id]

            try:
                result_data = json.loads(result_text)
                if result_data.get("error"):
                    msg = f"❌ **Execution failed:** {result_data['error']}"
                else:
                    msg = (
                        f"✅ **Operation executed successfully!**\n\n"
                        f"```json\n{json.dumps(result_data, indent=2)}\n```"
                    )
            except json.JSONDecodeError:
                msg = f"✅ **Operation executed.**\n\n{result_text}"

            self._add_to_history("assistant", msg)
            return {"type": "response", "message": msg}

        except Exception as exc:
            logger.error("execute_approved_action failed: %s", exc)
            # Always clean up — don't leave zombie pending actions
            self.pending_actions.pop(action_id, None)
            return {
                "type": "response",
                "message": f"❌ **Execution error:** {exc}",
            }

    async def cancel_action(self, action_id: str) -> dict:
        """
        Cancel a pending action (user clicked Cancel / No).

        Logs the denial and removes the action from pending state.
        """
        if action_id not in self.pending_actions:
            return {
                "type": "response",
                "message": (
                    "⚠️ **Action not found.** "
                    "It may have already expired or been processed."
                ),
            }

        action    = self.pending_actions.pop(action_id)
        tool_name = action["tool_name"]
        tool_args = action["tool_args"]

        target = (
            tool_args.get("deployment_name")
            or tool_args.get("resource_name")
            or tool_args.get("node_name")
            or "Unknown"
        )
        namespace = tool_args.get("namespace", "cluster")
        log_approval_denied(tool_name, target, namespace)

        msg = (
            f"🚫 **Operation cancelled.**  "
            f"The `{tool_name}` operation on **{target}** was not executed."
        )
        self._add_to_history("assistant", msg)
        return {"type": "response", "message": msg}

    def cleanup_expired_actions(self) -> None:
        """Evict any pending actions that have passed the timeout."""
        now     = time.time()
        expired = [
            aid for aid, action in self.pending_actions.items()
            if now - action["timestamp"] > PENDING_ACTION_TIMEOUT_SECONDS
        ]
        for aid in expired:
            logger.info("Evicting expired pending action %s", aid)
            del self.pending_actions[aid]
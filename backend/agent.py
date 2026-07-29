"""
backend/agent.py
────────────────────────────────────────
The AI brain. Connects Groq / Claude / Gemini to the MCP Server.
Manages the conversation loop, tool calling, and pending
approval state for write operations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from typing import Any, Optional

import httpx
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
from server.guardrails import is_write_operation, is_destructive_operation
from server.audit_log import log_approval_given, log_approval_denied

logger = logging.getLogger("kubeguardian.agent")

# ──────────────────────────────────────────────
# System prompt for the AI
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
    "scale_deployment": "execute_scale_deployment",
    "restart_deployment": "execute_restart_deployment",
    "patch_resource_labels": "execute_patch_resource",
    "cordon_node": "execute_cordon_node",
}


# ──────────────────────────────────────────────
# MCPGroqAgent
# ──────────────────────────────────────────────

class MCPGroqAgent:
    """
    AI Agent that bridges Groq/Claude/Gemini LLM with the
    MCP Server for Kubernetes management.
    """

    def __init__(self):
        """Initialize the agent (LLM client, MCP session placeholders)."""
        self.provider = AI_PROVIDER
        self._ai_client = None
        self._model = None
        self._init_ai_client()

        # MCP session (set during connect)
        self._mcp_session: Optional[ClientSession] = None
        self._mcp_streams = None

        # Conversation history (last 10 messages)
        self.conversation_history: list[dict] = []
        self.max_history = 10

        # Pending actions waiting for user approval
        self.pending_actions: dict[str, dict] = {}

        # Cached tool definitions
        self._tools_cache: list[dict] | None = None

    def _init_ai_client(self):
        """Initialize the appropriate AI client based on provider setting."""
        if self.provider == "groq":
            from groq import Groq
            self._ai_client = Groq(api_key=GROQ_API_KEY)
            self._model = GROQ_MODEL
            logger.info("AI Provider: Groq (model: %s)", self._model)
        elif self.provider == "claude":
            import anthropic
            self._ai_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
            self._model = CLAUDE_MODEL
            logger.info("AI Provider: Claude (model: %s)", self._model)
        elif self.provider == "gemini":
            import google.generativeai as genai
            genai.configure(api_key=GEMINI_API_KEY)
            self._ai_client = genai
            self._model = GEMINI_MODEL
            logger.info("AI Provider: Gemini (model: %s)", self._model)
        else:
            raise ValueError(f"Unknown AI provider: {self.provider}")

    # ────────────── MCP Connection ──────────────

    async def connect_mcp(self):
        """
        Establish a long-lived connection to the MCP Server
        via streamable HTTP transport.
        """
        logger.info("Connecting to MCP Server at %s", MCP_SERVER_URL)
        self._mcp_streams = streamablehttp_client(MCP_SERVER_URL)
        read_stream, write_stream, _ = await self._mcp_streams.__aenter__()
        self._mcp_session = ClientSession(read_stream, write_stream)
        await self._mcp_session.__aenter__()
        await self._mcp_session.initialize()
        logger.info("Connected to MCP Server successfully")

        # Cache tool definitions
        await self._refresh_tools()

    async def disconnect_mcp(self):
        """Close the MCP session and streams."""
        try:
            if self._mcp_session:
                await self._mcp_session.__aexit__(None, None, None)
            if self._mcp_streams:
                await self._mcp_streams.__aexit__(None, None, None)
        except Exception as e:
            logger.warning("Error disconnecting MCP: %s", e)

    async def _refresh_tools(self):
        """Fetch tool definitions from MCP and cache them."""
        tools_result = await self._mcp_session.list_tools()
        self._tools_cache = self._convert_tools_for_ai(tools_result.tools)
        logger.info("Loaded %d MCP tools", len(self._tools_cache))

    def _convert_tools_for_ai(self, mcp_tools) -> list[dict]:
        """
        Convert MCP tool definitions to the format expected
        by the AI provider (OpenAI-compatible for Groq/Gemini).

        Args:
            mcp_tools: List of MCP Tool objects.

        Returns:
            List of tool spec dicts.
        """
        tools = []
        for tool in mcp_tools:
            # Skip internal execute_ tools — they shouldn't be
            # called directly by the AI
            if tool.name.startswith("execute_"):
                continue

            # Build parameter schema
            properties = {}
            required = []
            if tool.inputSchema and "properties" in tool.inputSchema:
                for name, schema in tool.inputSchema["properties"].items():
                    properties[name] = {
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

    # ────────────── Conversation Management ──────────────

    def _add_to_history(self, role: str, content: str):
        """Add a message to conversation history, keeping last N."""
        self.conversation_history.append({"role": role, "content": content})
        # Keep system + last N user/assistant messages
        if len(self.conversation_history) > self.max_history * 2:
            self.conversation_history = self.conversation_history[-(self.max_history * 2):]

    # ────────────── Main Processing ──────────────

    async def process_message(self, user_message: str) -> dict:
        """
        Process a user message through the AI + MCP pipeline.

        Args:
            user_message: The user's natural language input.

        Returns:
            Dict with type ('response' or 'APPROVAL_REQUIRED'),
            message, and optional action details.
        """
        self._add_to_history("user", user_message)
        self.cleanup_expired_actions()

        try:
            if self.provider == "groq":
                return await self._process_groq(user_message)
            elif self.provider == "claude":
                return await self._process_claude(user_message)
            elif self.provider == "gemini":
                return await self._process_gemini(user_message)
        except Exception as e:
            logger.error("process_message failed: %s", e, exc_info=True)
            error_msg = f"❌ **Error processing your request:** {str(e)}"
            self._add_to_history("assistant", error_msg)
            return {"type": "response", "message": error_msg}

    async def _process_groq(self, user_message: str) -> dict:
        """Process via Groq API with tool calling."""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *self.conversation_history,
        ]

        response = self._ai_client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=self._tools_cache,
            tool_choice="auto",
            max_tokens=4096,
        )

        response_message = response.choices[0].message

        # If no tool calls, return direct response
        if not response_message.tool_calls:
            content = response_message.content or ""
            self._add_to_history("assistant", content)
            return {"type": "response", "message": content}

        # Process tool calls
        return await self._handle_tool_calls_groq(messages, response_message)

    async def _handle_tool_calls_groq(self, messages: list, response_message) -> dict:
        """
        Handle Groq tool calls — execute read tools directly,
        gate write tools behind approval.
        """
        # Add assistant message with tool calls to conversation
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

            logger.info("Tool call: %s(%s)", tool_name, tool_args)

            # Call the MCP tool
            mcp_result = await self._mcp_session.call_tool(tool_name, tool_args)
            result_text = ""
            for content_item in mcp_result.content:
                if hasattr(content_item, 'text'):
                    result_text += content_item.text

            # Check if this is a write operation and result contains pre_check
            if is_write_operation(tool_name):
                try:
                    result_data = json.loads(result_text)
                except json.JSONDecodeError:
                    result_data = {}

                if result_data.get("blocked"):
                    # Guardrail blocked it — return the reason
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_text,
                    })
                    # Get AI to explain the block
                    final = self._ai_client.chat.completions.create(
                        model=self._model,
                        messages=messages,
                        max_tokens=2048,
                    )
                    msg = final.choices[0].message.content or ""
                    self._add_to_history("assistant", msg)
                    return {"type": "response", "message": msg}

                if result_data.get("pre_check"):
                    # This is a write operation needing approval
                    action_id = str(uuid.uuid4())
                    self.pending_actions[action_id] = {
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                        "pre_check_result": result_data,
                        "timestamp": time.time(),
                    }

                    return {
                        "type": "APPROVAL_REQUIRED",
                        "action_id": action_id,
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                        "action_plan": {
                            "operation": result_data.get("operation", tool_name),
                            "target": result_data.get("deployment",
                                      result_data.get("resource_name",
                                      result_data.get("node_name", "Unknown"))),
                            "namespace": result_data.get("namespace", ""),
                            "current_state": (
                                f"{result_data.get('current_replicas', 'N/A')} replicas"
                                if "current_replicas" in result_data
                                else str(result_data.get("current_unschedulable", ""))
                            ),
                            "proposed_change": result_data.get("action_plan", ""),
                            "risk_level": result_data.get("risk_level", "LOW"),
                            "impact": result_data.get("impact", ""),
                            "pre_check_results": result_data.get("risks", []),
                        },
                    }

            # Read operation or non-pre-check — feed result back to Groq
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result_text,
            })

        # Get final response from Groq with tool results
        final_response = self._ai_client.chat.completions.create(
            model=self._model,
            messages=messages,
            max_tokens=4096,
        )
        content = final_response.choices[0].message.content or ""
        self._add_to_history("assistant", content)
        return {"type": "response", "message": content}

    async def _process_claude(self, user_message: str) -> dict:
        """Process via Claude API with tool calling."""
        # Convert tools to Claude format
        claude_tools = []
        for tool in (self._tools_cache or []):
            claude_tools.append({
                "name": tool["function"]["name"],
                "description": tool["function"]["description"],
                "input_schema": tool["function"]["parameters"],
            })

        messages = []
        for msg in self.conversation_history:
            messages.append({"role": msg["role"], "content": msg["content"]})

        response = self._ai_client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=claude_tools,
            messages=messages,
        )

        # Check for tool use
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_use_blocks:
            text_content = "".join(
                b.text for b in response.content if b.type == "text"
            )
            self._add_to_history("assistant", text_content)
            return {"type": "response", "message": text_content}

        # Process tool calls
        for tool_block in tool_use_blocks:
            tool_name = tool_block.name
            tool_args = tool_block.input

            logger.info("Tool call (Claude): %s(%s)", tool_name, tool_args)

            mcp_result = await self._mcp_session.call_tool(tool_name, tool_args)
            result_text = ""
            for content_item in mcp_result.content:
                if hasattr(content_item, 'text'):
                    result_text += content_item.text

            if is_write_operation(tool_name):
                try:
                    result_data = json.loads(result_text)
                except json.JSONDecodeError:
                    result_data = {}

                if result_data.get("blocked"):
                    msg = f"⚠️ **Operation Blocked:** {result_data.get('reason', 'Unknown reason')}"
                    self._add_to_history("assistant", msg)
                    return {"type": "response", "message": msg}

                if result_data.get("pre_check"):
                    action_id = str(uuid.uuid4())
                    self.pending_actions[action_id] = {
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                        "pre_check_result": result_data,
                        "timestamp": time.time(),
                    }
                    return {
                        "type": "APPROVAL_REQUIRED",
                        "action_id": action_id,
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                        "action_plan": {
                            "operation": result_data.get("operation", tool_name),
                            "target": result_data.get("deployment",
                                      result_data.get("resource_name",
                                      result_data.get("node_name", "Unknown"))),
                            "namespace": result_data.get("namespace", ""),
                            "current_state": (
                                f"{result_data.get('current_replicas', 'N/A')} replicas"
                                if "current_replicas" in result_data
                                else str(result_data.get("current_unschedulable", ""))
                            ),
                            "proposed_change": result_data.get("action_plan", ""),
                            "risk_level": result_data.get("risk_level", "LOW"),
                            "impact": result_data.get("impact", ""),
                            "pre_check_results": result_data.get("risks", []),
                        },
                    }

            # Send tool result back to Claude
            messages.append({"role": "assistant", "content": response.content})
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": result_text,
                }],
            })

        # Get final response
        final = self._ai_client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        text_content = "".join(
            b.text for b in final.content if b.type == "text"
        )
        self._add_to_history("assistant", text_content)
        return {"type": "response", "message": text_content}

    async def _process_gemini(self, user_message: str) -> dict:
        """Process via Gemini API with function calling."""
        # For Gemini, we use a simpler approach: call tools manually
        # and feed results back
        model = self._ai_client.GenerativeModel(
            self._model,
            system_instruction=SYSTEM_PROMPT,
        )

        # Build Gemini tools
        gemini_tools = []
        for tool in (self._tools_cache or []):
            func_decl = self._ai_client.protos.FunctionDeclaration(
                name=tool["function"]["name"],
                description=tool["function"]["description"],
                parameters=self._convert_params_to_gemini(tool["function"]["parameters"]),
            )
            gemini_tools.append(func_decl)

        tool_config = self._ai_client.protos.Tool(function_declarations=gemini_tools)

        # Build chat history
        history = []
        for msg in self.conversation_history[:-1]:  # Exclude current message
            role = "user" if msg["role"] == "user" else "model"
            history.append({"role": role, "parts": [msg["content"]]})

        chat = model.start_chat(history=history)
        response = chat.send_message(
            user_message,
            tools=[tool_config],
        )

        # Check for function calls
        if response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if hasattr(part, 'function_call') and part.function_call:
                    fc = part.function_call
                    tool_name = fc.name
                    tool_args = dict(fc.args) if fc.args else {}

                    logger.info("Tool call (Gemini): %s(%s)", tool_name, tool_args)

                    mcp_result = await self._mcp_session.call_tool(tool_name, tool_args)
                    result_text = ""
                    for content_item in mcp_result.content:
                        if hasattr(content_item, 'text'):
                            result_text += content_item.text

                    if is_write_operation(tool_name):
                        try:
                            result_data = json.loads(result_text)
                        except json.JSONDecodeError:
                            result_data = {}

                        if result_data.get("blocked"):
                            msg = f"⚠️ **Operation Blocked:** {result_data.get('reason', 'Unknown')}"
                            self._add_to_history("assistant", msg)
                            return {"type": "response", "message": msg}

                        if result_data.get("pre_check"):
                            action_id = str(uuid.uuid4())
                            self.pending_actions[action_id] = {
                                "tool_name": tool_name,
                                "tool_args": tool_args,
                                "pre_check_result": result_data,
                                "timestamp": time.time(),
                            }
                            return {
                                "type": "APPROVAL_REQUIRED",
                                "action_id": action_id,
                                "tool_name": tool_name,
                                "tool_args": tool_args,
                                "action_plan": {
                                    "operation": result_data.get("operation", tool_name),
                                    "target": result_data.get("deployment",
                                              result_data.get("resource_name",
                                              result_data.get("node_name", "Unknown"))),
                                    "namespace": result_data.get("namespace", ""),
                                    "current_state": (
                                        f"{result_data.get('current_replicas', 'N/A')} replicas"
                                        if "current_replicas" in result_data
                                        else str(result_data.get("current_unschedulable", ""))
                                    ),
                                    "proposed_change": result_data.get("action_plan", ""),
                                    "risk_level": result_data.get("risk_level", "LOW"),
                                    "impact": result_data.get("impact", ""),
                                    "pre_check_results": result_data.get("risks", []),
                                },
                            }

                    # Feed result back to Gemini
                    func_response = self._ai_client.protos.Part(
                        function_response=self._ai_client.protos.FunctionResponse(
                            name=tool_name,
                            response={"result": result_text},
                        )
                    )
                    response = chat.send_message(func_response)

        # Extract text response
        text = response.text if hasattr(response, 'text') else str(response)
        self._add_to_history("assistant", text)
        return {"type": "response", "message": text}

    def _convert_params_to_gemini(self, params: dict) -> dict:
        """Convert OpenAI-style parameters to Gemini proto format."""
        return {
            "type": "OBJECT",
            "properties": {
                name: {
                    "type": schema.get("type", "STRING").upper(),
                    "description": schema.get("description", ""),
                }
                for name, schema in params.get("properties", {}).items()
            },
            "required": params.get("required", []),
        }

    # ────────────── Approval Flow ──────────────

    async def execute_approved_action(self, action_id: str) -> dict:
        """
        Execute a previously approved write operation.

        Args:
            action_id: UUID of the pending action.

        Returns:
            Execution result dict.
        """
        if action_id not in self.pending_actions:
            return {
                "type": "response",
                "message": "❌ **Action not found.** It may have expired or already been processed.",
            }

        action = self.pending_actions[action_id]

        # Check timeout
        elapsed = time.time() - action["timestamp"]
        if elapsed > PENDING_ACTION_TIMEOUT_SECONDS:
            del self.pending_actions[action_id]
            return {
                "type": "response",
                "message": (
                    f"❌ **Action expired.** The approval window of "
                    f"{PENDING_ACTION_TIMEOUT_SECONDS} seconds has passed. "
                    "Please request the operation again."
                ),
            }

        tool_name = action["tool_name"]
        tool_args = action["tool_args"]
        execute_tool = EXECUTE_TOOL_MAP.get(tool_name)

        if not execute_tool:
            del self.pending_actions[action_id]
            return {
                "type": "response",
                "message": f"❌ **No execute handler found for '{tool_name}'.**",
            }

        try:
            # Log approval
            target = tool_args.get("deployment_name",
                     tool_args.get("resource_name",
                     tool_args.get("node_name", "Unknown")))
            namespace = tool_args.get("namespace", "cluster")
            log_approval_given(tool_name, target, namespace)

            # Call the execute_ tool on MCP
            mcp_result = await self._mcp_session.call_tool(execute_tool, tool_args)
            result_text = ""
            for content_item in mcp_result.content:
                if hasattr(content_item, 'text'):
                    result_text += content_item.text

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

        except Exception as e:
            logger.error("execute_approved_action failed: %s", e)
            del self.pending_actions[action_id]
            return {
                "type": "response",
                "message": f"❌ **Execution error:** {str(e)}",
            }

    async def cancel_action(self, action_id: str) -> dict:
        """
        Cancel a pending action.

        Args:
            action_id: UUID of the pending action.

        Returns:
            Cancellation confirmation dict.
        """
        if action_id not in self.pending_actions:
            return {
                "type": "response",
                "message": "⚠️ **Action not found.** It may have already expired or been processed.",
            }

        action = self.pending_actions[action_id]
        tool_name = action["tool_name"]
        tool_args = action["tool_args"]

        target = tool_args.get("deployment_name",
                 tool_args.get("resource_name",
                 tool_args.get("node_name", "Unknown")))
        namespace = tool_args.get("namespace", "cluster")

        log_approval_denied(tool_name, target, namespace)
        del self.pending_actions[action_id]

        msg = f"🚫 **Operation cancelled.** The {tool_name} operation on '{target}' was not executed."
        self._add_to_history("assistant", msg)
        return {"type": "response", "message": msg}

    def cleanup_expired_actions(self):
        """Remove any pending actions older than the timeout."""
        now = time.time()
        expired = [
            aid for aid, action in self.pending_actions.items()
            if now - action["timestamp"] > PENDING_ACTION_TIMEOUT_SECONDS
        ]
        for aid in expired:
            logger.info("Expired pending action: %s", aid)
            del self.pending_actions[aid]

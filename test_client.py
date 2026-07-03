"""Minimal MCP client — proves the bridge serves the capture over MCP exactly
as Claude Code will consume it (initialize → list tools → call tool)."""

import asyncio
import base64

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main() -> None:
    async with streamablehttp_client("http://127.0.0.1:7777/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])

            res = await session.call_tool("get_latest_capture", {})
            for c in res.content:
                if c.type == "text":
                    print("\n--- TEXT CONTENT ---")
                    print(c.text)
                elif c.type == "image":
                    n = len(base64.b64decode(c.data))
                    print(f"\n--- IMAGE CONTENT --- mimeType={c.mimeType} decoded_bytes={n}")


if __name__ == "__main__":
    asyncio.run(main())

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class McpProtocolTests(unittest.TestCase):
    def test_tools_list_is_available_without_model_requests(self):
        with tempfile.TemporaryDirectory() as state:
            env = dict(os.environ)
            env["HARNESSPILOT_STATE"] = state

            async def list_tools():
                server = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "harnesspilot.server"],
                    env=env,
                )
                async with stdio_client(server) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        return await session.list_tools()

            listing = asyncio.run(asyncio.wait_for(list_tools(), timeout=30))
            names = {tool.name for tool in listing.tools}
            self.assertEqual(names, {
                "hp_scan_project", "hp_list_tasks", "hp_estimate_budget", "hp_start_experiment",
                "hp_probe", "hp_get_status", "hp_cancel", "hp_resume", "hp_get_report",
                "hp_preview_apply", "hp_apply_candidate", "hp_preview_rollback", "hp_rollback",
            })


if __name__ == "__main__":
    unittest.main()

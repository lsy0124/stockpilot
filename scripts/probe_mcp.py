"""本地 MCP server 验证：起一个只读 SQLite 查询 MCP server，
用 fastmcp in-memory client 调一遍，确认"本地数据可通过 MCP 暴露给模型"链路成立。
同时记录：MCP 是 stdio/sse/http 传输，需要公网可达才能被 ChatGPT 远程调用 → 这是核心限制证据。
"""
import asyncio
import json
import os
import sqlite3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "evidence", "mcp_probes.jsonl")
DB = os.path.join(BASE, "data", "probe_test.db")


def rec(**kw):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(kw, ensure_ascii=False, default=str) + "\n")
    print(json.dumps(kw, ensure_ascii=False, default=str)[:400])


def build_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE quotes (code TEXT, date TEXT, close REAL, amount REAL)")
    con.execute("INSERT INTO quotes VALUES ('510300','2026-10-16',4.236,5075528306.28)")
    con.execute("INSERT INTO quotes VALUES ('600519','2026-10-16',1329.0,5075528306.28)")
    con.commit()
    con.close()


async def main():
    build_db()
    from fastmcp import FastMCP
    from fastmcp.client import Client

    mcp = FastMCP("stockpilot-readonly")

    @mcp.tool()
    def query_sql(sql: str) -> list:
        """只读查询 StockPilot SQLite（仅允许 SELECT）"""
        if not sql.strip().lower().startswith("select"):
            raise ValueError("只允许 SELECT")
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        con.close()
        return rows

    @mcp.tool()
    def latest_close(code: str) -> dict:
        """查询某证券最新收盘价"""
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        cur = con.execute("SELECT * FROM quotes WHERE code=? ORDER BY date DESC LIMIT 1", (code,))
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        con.close()
        return dict(zip(cols, row)) if row else {}

    client = Client(mcp)
    async with client:
        tools = await client.list_tools()
        rec(stage="list_tools", ok=True, tools=[t.name for t in tools])
        r1 = await client.call_tool("latest_close", {"code": "510300"})
        rec(stage="call_tool latest_close", ok=True, result=r1.content[0].text if r1.content else None)
        r2 = await client.call_tool("query_sql", {"sql": "SELECT code, close FROM quotes"})
        rec(stage="call_tool query_sql", ok=True, result=r2.content[0].text if r2.content else None)
        try:
            await client.call_tool("query_sql", {"sql": "DELETE FROM quotes"})
            rec(stage="call_tool delete blocked", ok=False)
        except Exception as e:
            rec(stage="call_tool delete blocked", ok=True, error=str(e)[:120])


if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import asyncpg
import os

async def inspect():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    cols = await conn.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'policy_drafts' ORDER BY ordinal_position"
    )
    for c in cols:
        print(c['column_name'], '-', c['data_type'])
    await conn.close()

asyncio.run(inspect())

import asyncio
import asyncpg
import os

async def inspect():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    tables = await conn.fetch(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' ORDER BY table_name"
    )
    print('Tables:', [t['table_name'] for t in tables])
    await conn.close()

asyncio.run(inspect())

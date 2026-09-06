import asyncio
import asyncpg
import os

async def inspect():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    for table in ('bookings', 'flights'):
        print(f'--- {table} ---')
        cols = await conn.fetch(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = $1 ORDER BY ordinal_position",
            table,
        )
        for c in cols:
            print(c['column_name'], '-', c['data_type'])
        print()
    await conn.close()

asyncio.run(inspect())

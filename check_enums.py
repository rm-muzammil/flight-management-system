import asyncio
import asyncpg
import os

async def inspect():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    rows = await conn.fetch("""
        SELECT t.typname AS enum_name, e.enumlabel AS value
        FROM pg_type t
        JOIN pg_enum e ON t.oid = e.enumtypid
        ORDER BY t.typname, e.enumsortorder
    """)
    current = None
    for r in rows:
        if r['enum_name'] != current:
            current = r['enum_name']
            print(f"\n{current}:")
        print(" -", r['value'])
    await conn.close()

asyncio.run(inspect())

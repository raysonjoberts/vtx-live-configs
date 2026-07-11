import oracledb

# === DB Connection Info ===
username = 'APPRATBAT'
password = 'x65U9e4!yB45-7hNt'
host = '10.210.144.41'
port = 1521
service = 'AINVPROD'  # Oracle service name
dsn = f"{host}:{port}/{service}"

# === Enable Oracle Thick Mode (use your actual Instant Client folder) ===
oracledb.init_oracle_client(lib_dir=r"C:\oracle\instantclient-basic-windows.x64-23.8.0.25.04\instantclient_23_8")

# === Connect to the DB ===
connection = oracledb.connect(user=username, password=password, dsn=dsn)
cursor = connection.cursor()

# === Query tables in schema AINVPROD ===
target_schema = 'APPINVDBA'

cursor.execute("""
    SELECT table_name
    FROM all_tables
    WHERE owner = :owner
    ORDER BY table_name
""", [target_schema])

results = cursor.fetchall()

if results:
    print(f"Tables in schema '{target_schema}':")
    for table_name, in results:
        print(f" - {table_name}")
else:
    print(f"No tables found in schema '{target_schema}'")

# === Cleanup ===
cursor.close()
connection.close()

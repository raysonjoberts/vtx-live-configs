import oracledb
import pandas as pd

# === Connection Details ===
username = 'APPRATBAT'
password = 'x65U9e4!yB45-7hNt'
host = '10.210.144.41'
port = 1521
service = 'AINVPROD'
dsn = f"{host}:{port}/{service}"

# === Use Oracle Instant Client for Thick Mode ===
oracledb.init_oracle_client(lib_dir=r"C:\oracle\instantclient-basic-windows.x64-23.8.0.25.04\instantclient_23_8")

# === Connect to Oracle ===
connection = oracledb.connect(user=username, password=password, dsn=dsn)

# === Table you want to export (from AINVPROD schema) ===
schema = 'APPINVDBA'
table = 'APPLICATIONS'  # << replace with an actual table name

# === Read table into DataFrame ===
query = f'SELECT * FROM {schema}.{table}'
df = pd.read_sql(query, con=connection)

# === Export to CSV ===
output_dir = r"C:\BTDM_7.1\var"
output_path = f"{output_dir}\\{table}.csv"
df.to_csv(output_path, index=False)
print(f"Exported {table} to {output_path}")

# === Cleanup ===
connection.close()

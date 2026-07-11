import oracledb
import pandas as pd
import csv  # For CSV quoting constants
import time
import os
import traceback

# === Connection Details ===
username = 'APPRATBAT'
password = 'x65U9e4!yB45-7hNt'
host = '10.210.144.41'
port = 1521
service = 'AINVPROD'
dsn = f"{host}:{port}/{service}"

# === Oracle Client Init ===
oracledb.init_oracle_client(lib_dir=r"C:\oracle\instantclient-basic-windows.x64-23.8.0.25.04\instantclient_23_8")

# === Output Directory ===
output_dir = r"C:\BTDM_7.1\var\customerdata"
os.makedirs(output_dir, exist_ok=True)

# === Table List ===
tables = [
    "V_CCOE_APPLICATION_STAFF"
]

schema = 'APPINVDBA'

# === Data Sanitization ===
def sanitize_multiline_fields(df):
    for col in df.select_dtypes(include=['object']).columns:
        df[col] = df[col].astype(str).str.replace('\r', ' ', regex=False)
        df[col] = df[col].str.replace('\n', ' ', regex=False)
    return df

# === Connect to Oracle ===
print("Connecting to Oracle database...")
connection = oracledb.connect(user=username, password=password, dsn=dsn)
print("Connection established.\n")

successful = []
failed = []

try:
    for table in tables:
        print(f"=== Processing table: {table} ===")
        try:
            query = f'SELECT * FROM {schema}.{table}'
            df = pd.read_sql(query, con=connection)

            df = sanitize_multiline_fields(df)  # Clean up line breaks before export

            output_path = os.path.join(output_dir, f"{table}.csv")
            df.to_csv(
                output_path,
                index=False,
                quoting=csv.QUOTE_ALL,
                escapechar='\\',
                lineterminator='\n'
            )

            print(f"[SUCCESS] Exported {table} ({len(df)} rows, {len(df.columns)} columns) to {output_path}")
            successful.append(table)
        except Exception as e:
            print(f"[ERROR] Failed to export {table}: {e}")
            traceback.print_exc()
            failed.append(table)

        print(f"Sleeping for 10 seconds...\n")
        time.sleep(10)

finally:
    connection.close()
    print("Connection closed.\n")

# === Summary ===
print("=== Export Summary ===")
print(f"Successful exports: {len(successful)}")
if successful:
    print(" - " + "\n - ".join(successful))

if failed:
    print(f"\nFailed exports: {len(failed)}")
    print(" - " + "\n - ".join(failed))
else:
    print("\nAll tables exported successfully.")

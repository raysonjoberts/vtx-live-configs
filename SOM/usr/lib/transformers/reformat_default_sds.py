"""
Transformer: reformat_default_sds.py

This transformer standardizes server identifiers by creating a normalized 'Server'
column based on a configured input field, typically a fully qualified domain name (FQDN).
If the field is already named 'Server', the original is preserved as 'Server_old'.

Typical Use Case:
- Ensures consistency across SDS datasets with varying server field names.
- Normalizes server names to lowercase hostnames without domain suffixes.

Configuration Required:
- `server_field` must be specified in the transforms dictionary.

Input:
- DataFrame with a column containing server names or FQDNs.

Output:
- DataFrame with a lowercase, hostname-only 'Server' column.
- Original 'Server' column renamed to 'Server_old' if it was transformed.
"""

def reformat_default_sds(df, transforms):
    server_field = transforms.get("server_field")
    if not server_field:
        raise ValueError("Missing 'server_field' in datatransform.conf for SDS transform")

    if server_field not in df.columns:
        raise ValueError(f"'{server_field}' not found in input data columns")

    if "Server" in df.columns and server_field == "Server":
        df["Server_new"] = df["Server"].str.lower().str.split(".").str[0]
        df.rename(columns={"Server": "Server_old", "Server_new": "Server"}, inplace=True)
    else:
        df["Server"] = df[server_field].str.lower().str.split(".").str[0]

    return df

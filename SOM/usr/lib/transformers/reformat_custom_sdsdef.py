"""
Transformer: reformat_custom_sdsdef.py

This transformer standardizes server identifiers by extracting the hostname portion
from a fully qualified domain name (FQDN). The source field is defined by the
`server_field` value in `datatransform.conf`.

If the source field is already named 'Server', the original column is preserved as
'Server_old' and replaced by the normalized version.

Typical Use Case:
- Normalizes server names to bare hostnames for consistency.
- Ensures a standardized 'Server' column is always present.

Configuration Required:
- `server_field` must be specified in the transforms dictionary.

Input:
- DataFrame with a field containing FQDN or server identifiers.

Output:
- DataFrame with a lowercase, hostname-only 'Server' column.
- Original 'Server' column is preserved as 'Server_old' if applicable.
"""


def reformat_custom_sdsdef(df, transforms):
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

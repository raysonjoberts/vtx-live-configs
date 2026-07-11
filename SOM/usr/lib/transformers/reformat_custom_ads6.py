"""
Transformer: reformat_custom_ads6.py

This transformer ensures the presence of a 'DATABASE_ID' column by copying the value from a
configured field if needed. The specific source field is defined in `datatransform.conf`
under the key `transform_field`. If 'DATABASE_ID' is already present and matches the transform
field, no change is made.

Typical Use Case:
- Standardizes database identifiers under a common 'DATABASE_ID' column.
- Useful when source files label the database field inconsistently.

Configuration Required:
- `transform_field` must be specified in the transforms dictionary.

Input:
- Any DataFrame containing the configured identifier column.

Output:
- Same DataFrame with an added or preserved 'DATABASE_ID' column.
"""


def reformat_custom_ads6(df, transforms):
    transform_field = transforms.get("transform_field")
    if not transform_field:
        raise ValueError("Missing 'transform_field' in datatransform.conf for ADS transform")

    if transform_field not in df.columns:
        raise ValueError(f"'{transform_field}' not found in input data columns")

    # Only assign to DATABASE_ID if it doesn't already match the expected field
    if not ("DATABASE_ID" in df.columns and transform_field == "DATABASE_ID"):
        df["DATABASE_ID"] = df[transform_field]

    return df

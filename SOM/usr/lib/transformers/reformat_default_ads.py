"""
Transformer: reformat_default_ads.py

This transformer ensures the presence of an 'Application' column by copying values
from a configured source field, defined as `app_field` in `datatransform.conf`.
If the input already contains 'Application' and it matches the transform field,
no reassignment is performed.

Typical Use Case:
- Aligns differing source schemas to a common 'Application' identifier field.
- Ensures consistent application tagging across ADS inputs.

Configuration Required:
- `app_field` must be specified in the transforms dictionary.

Input:
- DataFrame with the configured application identifier column.

Output:
- DataFrame with an added or standardized 'Application' column.
"""

def reformat_default_ads(df, transforms):
    app_field = transforms.get("app_field")
    if not app_field:
        raise ValueError("Missing 'app_field' in datatransform.conf for ADS transform")

    if app_field not in df.columns:
        raise ValueError(f"'{app_field}' not found in input data columns")

    # Only assign to Application if it doesn't already match the expected field
    if not ("Application" in df.columns and app_field == "Application"):
        df["Application"] = df[app_field]

    return df

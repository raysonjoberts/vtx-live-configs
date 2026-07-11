import pandas as pd

df1 = pd.read_csv(r"C:\BTDM_7.1\var\tables\bfms_hierarchy.csv")
df2 = pd.read_csv(r"C:\BTDM_7.1\var\tables\bfms_organizations.csv")

print(df1.compare(df2))
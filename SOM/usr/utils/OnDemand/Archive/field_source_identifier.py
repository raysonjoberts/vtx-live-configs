import os
import pandas as pd

def compile_field_sources(input_folder, output_csv):
    records = []

    for filename in os.listdir(input_folder):
        if filename.lower().endswith(".csv"):
            filepath = os.path.join(input_folder, filename)
            try:
                df = pd.read_csv(filepath, nrows=0)  # Only read headers
                for col in df.columns:
                    records.append({'Field': col.strip(), 'Source': filename})
            except Exception as e:
                print(f"❌ Error reading {filename}: {e}")

    # Convert list of dicts to DataFrame and write to CSV
    result_df = pd.DataFrame(records)
    result_df.sort_values(by=["Field", "Source"], inplace=True)
    result_df.to_csv(output_csv, index=False)
    print(f"✅ Field-source mapping written to {output_csv}")

# === Example usage ===
if __name__ == "__main__":
    input_folder = r"C:\BTDM_7.1\var\tables"  # Replace with your actual folder path
    output_csv = r"C:\BTDM_7.1\var\field_source_summary.csv"
    compile_field_sources(input_folder, output_csv)

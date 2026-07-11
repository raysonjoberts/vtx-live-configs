import os
import argparse

def load_names_from_file(file_path):
    names = set()
    with open(file_path, 'r') as f:
        for line in f:
            parts = line.strip().split(',')
            if len(parts) >= 2:
                name, gender = parts[0], parts[1]
                names.add((name, gender))
    return names

def combine_ssa_names(data_dir, start_year, end_year):
    combined_names = set()

    for year in range(start_year, end_year + 1):
        filename = f"yob{year}.txt"
        file_path = os.path.join(data_dir, filename)
        if os.path.exists(file_path):
            year_names = load_names_from_file(file_path)
            combined_names.update(year_names)
        else:
            print(f"Warning: File not found for year {year}: {file_path}")

    return combined_names

def write_output(names, output_file):
    with open(output_file, 'w') as f:
        for name, gender in sorted(names):
            f.write(f"{name},{gender}\n")
    print(f"Combined list written to {output_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Combine SSA name files by year range.")
    parser.add_argument("--data-dir", required=True, help="Directory containing SSA yobYYYY.txt files")
    parser.add_argument("--start-year", type=int, required=True, help="Start year (e.g. 1980)")
    parser.add_argument("--end-year", type=int, required=True, help="End year (e.g. 2020)")
    parser.add_argument("--output", default="combined_names.txt", help="Output file name")

    args = parser.parse_args()
    names = combine_ssa_names(args.data_dir, args.start_year, args.end_year)
    write_output(names, args.output)

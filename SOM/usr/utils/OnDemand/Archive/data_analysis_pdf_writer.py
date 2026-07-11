import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import base64
from io import BytesIO
from datetime import datetime

# 🔹 User-defined paths
input_file = r"C:\BTDM_7.1\var\analysis\ais_report.csv"   # CSV file
output_file = r"C:\BTDM_7.1\var\analysis\ais_report.html" # HTML output

# Thresholds for RAG status
thresholds = {
    "value_match": {"green": 0.9, "amber": 0.7},
    "avg_length": {"green_max": 50, "amber_max": 100},
    "length_stddev": {"green_max": 20, "amber_max": 40},
    "avg_words": {"green_max": 5, "amber_max": 10},
    "words_stddev": {"green_max": 3, "amber_max": 6}
}

def rag_status(value, green, amber=None, higher_is_better=True):
    if higher_is_better:
        if value >= green: return "✅"
        elif amber and value >= amber: return "⚠"
        else: return "❌"
    else:
        if value <= green: return "✅"
        elif amber and value <= amber: return "⚠"
        else: return "❌"

# 🔹 Convert matplotlib figure to Base64 image
def plot_to_base64(fig):
    buf = BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    img_b64 = base64.b64encode(buf.read()).decode("utf-8")
    plt.close(fig)
    return f"data:image/png;base64,{img_b64}"

# 🔹 Chart generator with shading + indicators
def add_distribution_chart(mean, stddev, title, xlabel, thresholds=None):
    if stddev <= 0:
        return None
    
    # Generate distribution
    x = np.linspace(mean - 4*stddev, mean + 4*stddev, 500)
    y = (1/(stddev*np.sqrt(2*np.pi))) * np.exp(-0.5*((x-mean)/stddev)**2)

    fig, ax = plt.subplots()
    ax.plot(x, y, label="Normal Distribution")
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("Density")

    # 🔹 Mean indicator (red line)
    ax.axvline(mean, color="red", linestyle="-", linewidth=2, label="Mean")
    ax.text(mean, max(y)*0.9, "Mean", color="red", rotation=90, ha="right", va="center")

    # 🔹 Stddev range (blue dotted lines)
    ax.axvline(mean - stddev, color="blue", linestyle="--", linewidth=1.5, label="-1σ")
    ax.axvline(mean + stddev, color="blue", linestyle="--", linewidth=1.5, label="+1σ")
    ax.text(mean - stddev, max(y)*0.7, "-1σ", color="blue", rotation=90, ha="right", va="center")
    ax.text(mean + stddev, max(y)*0.7, "+1σ", color="blue", rotation=90, ha="left", va="center")

    # 🔹 Shade ±1σ area
    ax.fill_between(x, y, where=((x >= mean-stddev) & (x <= mean+stddev)),
                    color="blue", alpha=0.2, label="±1σ Range")

    # 🔹 "Good" stddev thresholds (green dotted), if provided
    if thresholds:
        ax.axvline(thresholds["green_max"], color="green", linestyle=":", linewidth=1.5, label="Good Threshold (+)")
        ax.text(thresholds["green_max"], max(y)*0.5, "Good Threshold", color="green", rotation=90, ha="left", va="center")

    ax.legend()
    return plot_to_base64(fig)

# Load your table
df = pd.read_csv(input_file)

df = df[df['Program Match'] == True]

# Build HTML report
html_parts = []
html_parts.append(f"<html><head><title>Analysis Report</title></head><body>")
html_parts.append(f"<h1>📊 Analysis Report</h1>")
html_parts.append(f"<p>Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p><hr>")

for idx, row in df.iterrows():
    field_name = row['Field Name']
    attr = row['Attribute']
    
    charts = {}

    # Length Distribution
    charts['length'] = add_distribution_chart(
        mean=row['Average Length'],
        stddev=row['Length stddev'],
        title="Length Distribution",
        xlabel="Length",
        thresholds=thresholds['length_stddev']
    )

    # Word Distribution
    charts['words'] = add_distribution_chart(
        mean=row['Average Words'],
        stddev=row['Words stddev'],
        title="Word Distribution",
        xlabel="Words",
        thresholds=thresholds['words_stddev']
    )

    # Add HTML section
    html_parts.append(f"<h2>📌 Field: {field_name}</h2>")
    html_parts.append(f"<p><b>Attribute:</b> {attr}</p>")
    html_parts.append("<ul>")
    html_parts.append(f"<li>Attribute Match: {'✅' if row['Program Match'] else '❌'}</li>")
    html_parts.append(f"<li>Value Match Ratio: {row['Value Match Ratio']:.1%} {rag_status(row['Value Match Ratio'], thresholds['value_match']['green'], thresholds['value_match']['amber'])}</li>")
    html_parts.append(f"<li>Average Length: {row['Average Length']:.2f} ± {row['Length stddev']:.2f} {rag_status(row['Average Length'], thresholds['avg_length']['green_max'], thresholds['avg_length']['amber_max'], higher_is_better=False)}</li>")
    html_parts.append(f"<li>Average Words: {row['Average Words']:.2f} ± {row['Words stddev']:.2f} {rag_status(row['Average Words'], thresholds['avg_words']['green_max'], thresholds['avg_words']['amber_max'], higher_is_better=False)}</li>")
    html_parts.append("</ul>")

    # Insert charts inline
    if charts['length'] or charts['words']:
        html_parts.append("<div style='display:flex; gap:20px;'>")
        if charts['length']:
            html_parts.append(f"<div><img src='{charts['length']}' width='350'></div>")
        if charts['words']:
            html_parts.append(f"<div><img src='{charts['words']}' width='350'></div>")
        html_parts.append("</div>")
    
    html_parts.append("<hr>")

html_parts.append("</body></html>")

# Save HTML
with open(output_file, "w", encoding="utf-8") as f:
    f.write("\n".join(html_parts))

print(f"✅ Standalone HTML report generated: {output_file}")

"""
Generates clean architecture diagrams for docs/PROJECT_REPORT.pdf using matplotlib.
"""

import matplotlib.pyplot as plt
import matplotlib.patches as patches


def create_request_flow_diagram(output_path: str = "docs/request_flow_diagram.png") -> None:
    fig, ax = plt.subplots(figsize=(10.5, 4.8), dpi=300)
    ax.axis("off")
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 50)

    # Style definitions
    box_style = dict(boxstyle="round,pad=0.5", facecolor="#EBF3FB", edgecolor="#2B6CB0", lw=1.5)
    service_style = dict(boxstyle="round,pad=0.5", facecolor="#E6FFFA", edgecolor="#234E52", lw=1.5)
    external_style = dict(boxstyle="round,pad=0.5", facecolor="#FFF5F5", edgecolor="#9B2C2C", lw=1.5, ls="--")
    cache_style = dict(boxstyle="round,pad=0.4", facecolor="#FEFCBF", edgecolor="#744210", lw=1.2)

    # Top row
    ax.text(10, 38, "Postman / Client\nPOST /api/route/\n{'start', 'finish'}", ha="center", va="center", bbox=box_style, fontsize=8.5, fontweight="bold")
    ax.text(35, 38, "RouteView & Serializer\nValidate input fields &\nensure start != finish", ha="center", va="center", bbox=box_style, fontsize=8.5)
    ax.text(65, 38, "routing_client\nget_route(start, finish)\n1 OSRM call + LRU cache", ha="center", va="center", bbox=service_style, fontsize=8.5, fontweight="bold")
    ax.text(90, 38, "fuel_planner\nplan_fuel_stops()\nKD-Tree + Lazy Greedy", ha="center", va="center", bbox=service_style, fontsize=8.5, fontweight="bold")

    # Middle / side helper components
    ax.text(65, 20, "External APIs (OSRM / Nominatim)\nCalled ONCE per route (0 on cached repeat)", ha="center", va="center", bbox=external_style, fontsize=7.5)
    ax.text(90, 20, "In-Memory cKDTree\n6,626 Stations (Loaded Once)", ha="center", va="center", bbox=cache_style, fontsize=7.5)

    # Bottom row
    ax.text(65, 7, "response_builder\nbuild_route_response()\nGeoJSON + Timing Meta", ha="center", va="center", bbox=service_style, fontsize=8.5)
    ax.text(35, 7, "RouteView Response\nJSON 200 / 400 / 422 / 502\n(Fast Repeat < 100ms)", ha="center", va="center", bbox=box_style, fontsize=8.5, fontweight="bold")

    # Arrows
    arrow_kw = dict(arrowstyle="->", lw=1.8, color="#2D3748")
    dash_arrow = dict(arrowstyle="<->", lw=1.4, color="#718096", linestyle="--")

    # Top pipeline
    ax.annotate("", xy=(24, 38), xytext=(19, 38), arrowprops=arrow_kw)
    ax.annotate("", xy=(52, 38), xytext=(47, 38), arrowprops=arrow_kw)
    ax.annotate("", xy=(79, 38), xytext=(76, 38), arrowprops=arrow_kw)

    # routing_client to external APIs
    ax.annotate("", xy=(65, 26), xytext=(65, 31), arrowprops=dash_arrow)

    # fuel_planner to cKDTree
    ax.annotate("", xy=(90, 26), xytext=(90, 31), arrowprops=dash_arrow)

    # cKDTree / fuel_planner down to response_builder
    ax.annotate("", xy=(77, 12), xytext=(85, 15), arrowprops=arrow_kw)

    # response_builder left to RouteView Response
    ax.annotate("", xy=(48, 7), xytext=(53, 7), arrowprops=arrow_kw)

    # RouteView Response up-left to Postman / Client
    ax.annotate("", xy=(10, 29), xytext=(25, 12), arrowprops=arrow_kw)

    plt.tight_layout()
    plt.savefig(output_path, bbox_inches="tight")
    plt.close()
    print(f"Created {output_path}")


def create_pipeline_diagram(output_path: str = "docs/pipeline_diagram.png") -> None:
    fig, ax = plt.subplots(figsize=(10, 3.8), dpi=300)
    ax.axis("off")
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 40)

    # Box styles
    file_style = dict(boxstyle="round,pad=0.5", facecolor="#EDF2F7", edgecolor="#4A5568", lw=1.5)
    step_style = dict(boxstyle="round,pad=0.5", facecolor="#EBF8FF", edgecolor="#3182CE", lw=1.5)
    db_style = dict(boxstyle="round,pad=0.5", facecolor="#FAF5FF", edgecolor="#6B46C1", lw=1.5)
    mem_style = dict(boxstyle="round,pad=0.5", facecolor="#F0FFF4", edgecolor="#276749", lw=1.5)

    ax.text(10, 20, "OPIS CSV\n8,151 rows\nRetail Prices", ha="center", va="center", bbox=file_style, fontsize=9, fontweight="bold")
    ax.text(28, 20, "1. Filter US\nDrop non-US (620)\nKeep 50 States + DC", ha="center", va="center", bbox=step_style, fontsize=8.5)
    ax.text(46, 20, "2. Dedupe\nMerge 905 dupes\nLowest price wins", ha="center", va="center", bbox=step_style, fontsize=8.5)
    ax.text(64, 20, "3. Offline Geocode\ngeonamescache cities\n0 network calls", ha="center", va="center", bbox=step_style, fontsize=8.5)
    ax.text(82, 28, "4. Station Table\nSQLite (bulk_create)\nIndex on (lat, lng)", ha="center", va="center", bbox=db_style, fontsize=8.5, fontweight="bold")
    ax.text(82, 10, "5. SciPy cKDTree\nIn-Memory Singleton\n6,626 Station Coords", ha="center", va="center", bbox=mem_style, fontsize=8.5, fontweight="bold")

    arrow_kw = dict(arrowstyle="->", lw=1.8, color="#2D3748")
    ax.annotate("", xy=(20, 20), xytext=(17, 20), arrowprops=arrow_kw)
    ax.annotate("", xy=(38, 20), xytext=(35.5, 20), arrowprops=arrow_kw)
    ax.annotate("", xy=(56, 20), xytext=(53.5, 20), arrowprops=arrow_kw)
    ax.annotate("", xy=(73, 26), xytext=(71.5, 22), arrowprops=arrow_kw)
    ax.annotate("", xy=(82, 16), xytext=(82, 22), arrowprops=arrow_kw)

    plt.tight_layout()
    plt.savefig(output_path, bbox_inches="tight")
    plt.close()
    print(f"Created {output_path}")


if __name__ == "__main__":
    create_request_flow_diagram()
    create_pipeline_diagram()

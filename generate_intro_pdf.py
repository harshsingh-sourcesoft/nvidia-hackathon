"""Generates PlanLift feature introduction PDF (latin-1 safe)."""
from fpdf import FPDF
from fpdf.enums import XPos, YPos

ACCENT = (59, 130, 246)
DARK   = (15, 23, 42)
MID    = (71, 85, 105)
LIGHT  = (241, 245, 249)
WHITE  = (255, 255, 255)

SECTIONS = [
    {
        "num": "01",
        "title": "Upload a Floor Plan",
        "desc": (
            "Drag and drop any floor plan image (PNG, JPG, BMP) into the Upload tab. "
            "PlanLift's AI immediately analyses it to detect walls, doors, and windows automatically."
        ),
        "bullets": [
            "Single or multi-section images supported",
            "Element count shown after analysis: Walls / Doors / Windows",
            "Multi-floor buildings: set total floors and floor height once -- each floor stacks automatically",
        ],
    },
    {
        "num": "02",
        "title": "Edit Tab -- Refine the Floor Plan",
        "desc": (
            "After analysis, switch to the Edit tab to review and correct every detected element "
            "before moving to 3D."
        ),
        "bullets": [
            "Toolbar: Select, Wall, Door, Window, Undo, Delete, Clear",
            "Click any element to see its real-world dimensions (m / ft)",
            "Add, move, or delete walls, doors and windows with a single click",
            "Calibrate scale: enter a known door width to set 1 px = X mm for the whole plan",
        ],
    },
    {
        "num": "03",
        "title": "3D View -- Instant Visualisation",
        "desc": (
            "One click takes the corrected floor plan into a live 3D view. "
            "Explore the building from any angle with full orbit, pan, and zoom."
        ),
        "bullets": [
            "Camera presets: Perspective, Top, Front",
            "Toggle Wireframe, Hide Ceiling, Hide Floor independently",
            "Reset Camera returns to the default overview instantly",
            "Unit switcher (m / ft) updates all displayed measurements live",
        ],
    },
    {
        "num": "04",
        "title": "Wall Textures -- PolyHaven PBR Materials",
        "desc": (
            "Select any wall in the 3D view to open the texture panel. "
            "Search thousands of free, high-quality PBR textures from PolyHaven."
        ),
        "bullets": [
            "Search by keyword (e.g. brick, concrete, plaster)",
            "Apply to Selected Wall or Apply to All Walls with one button",
            "UV mapping controls: Scale X / Y and Rotate",
            "Global material preset (Brick, Plaster, Concrete) applies to the entire build",
        ],
    },
    {
        "num": "05",
        "title": "Furniture and 3D Models",
        "desc": (
            "Populate the scene with furniture from three sources, all accessible from the right panel."
        ),
        "bullets": [
            "Sketchfab -- search millions of ready-to-place 3D models by tag",
            "Image to 3D -- upload 1-4 photos and generate a custom model",
            "Text to 3D -- describe an object in plain English to generate it",
            "Drag a thumbnail to place; Recent Models keeps your last picks handy",
        ],
    },
    {
        "num": "06",
        "title": "Open in Isaac Sim",
        "desc": (
            "Export the complete scene -- building, furniture, materials -- directly into "
            "NVIDIA Isaac Sim for physics simulation, robotics testing, and photorealistic rendering."
        ),
        "bullets": [
            "One-click export via the Open in Isaac Sim button in 3D View",
            "Furniture models are converted to USD format automatically",
            "Glass doors and windows receive accurate OmniGlass materials",
            "The full USD stage is ready for simulation with no manual setup required",
        ],
    },
]


class PDF(FPDF):
    def header(self):
        pass

    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", size=8)
        self.set_text_color(*MID)
        self.cell(0, 8, f"PlanLift  -  Page {self.page_no()}", align="C")


def draw_cover(pdf: PDF):
    pdf.add_page()

    # Dark background
    pdf.set_fill_color(*DARK)
    pdf.rect(0, 0, 210, 297, "F")

    # Blue left bar
    pdf.set_fill_color(*ACCENT)
    pdf.rect(0, 0, 7, 297, "F")

    # "Plan" in white
    pdf.set_xy(22, 62)
    pdf.set_font("Helvetica", "B", 44)
    pdf.set_text_color(*WHITE)
    pdf.cell(0, 18, "Plan", new_x=XPos.END, new_y=YPos.TOP)

    # "Lift" in blue
    pdf.set_text_color(*ACCENT)
    pdf.cell(0, 18, "Lift", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # Tagline
    pdf.set_xy(22, 89)
    pdf.set_font("Helvetica", "I", 14)
    pdf.set_text_color(170, 195, 230)
    pdf.cell(0, 8, "From floor plan to simulation, in minutes.")

    # Divider
    pdf.set_draw_color(*ACCENT)
    pdf.set_line_width(0.4)
    pdf.line(22, 103, 190, 103)

    # Description paragraph
    pdf.set_xy(22, 110)
    pdf.set_font("Helvetica", size=11)
    pdf.set_text_color(160, 185, 215)
    desc = (
        "PlanLift is an AI-powered floor plan tool. Upload a floor plan image, "
        "edit the detected elements, explore a live 3D model, apply real-world textures, "
        "place furniture, and export the complete scene to NVIDIA Isaac Sim -- "
        "all from one interface."
    )
    pdf.multi_cell(168, 7, desc)

    # Feature chips
    chips = ["AI Detection", "3D Viewer", "PBR Textures", "Furniture", "Isaac Sim Export"]
    cx, cy = 22, 170
    for chip in chips:
        w = pdf.get_string_width(chip) + 12
        if cx + w > 188:
            cx = 22
            cy += 13
        pdf.set_fill_color(*ACCENT)
        pdf.set_text_color(*WHITE)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_xy(cx, cy)
        pdf.cell(w, 9, chip, fill=True, align="C")
        cx += w + 6

    # Version / date
    pdf.set_xy(22, 272)
    pdf.set_font("Helvetica", size=8)
    pdf.set_text_color(*MID)
    pdf.cell(0, 6, "Internal product overview  -  2026")


def draw_section(pdf: PDF, sec: dict, is_first: bool):
    if is_first:
        pdf.add_page()
        y = 22
    else:
        y = pdf.get_y() + 12
        if y + 68 > 274:
            pdf.add_page()
            y = 22

    # Number badge
    pdf.set_fill_color(*ACCENT)
    pdf.set_xy(14, y)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*WHITE)
    pdf.cell(13, 7, sec["num"], fill=True, align="C")

    # Title
    pdf.set_xy(31, y)
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(*DARK)
    pdf.cell(0, 7, sec["title"])

    y = y + 10

    # Estimate card height
    lh = 5.5
    desc_lines = len(pdf.multi_cell(172, lh, sec["desc"], dry_run=True, output="LINES"))
    card_h = 5 + desc_lines * lh + 3 + len(sec["bullets"]) * 7 + 5

    # Card background
    pdf.set_fill_color(*LIGHT)
    pdf.rect(14, y, 182, card_h, "F")

    # Accent left strip
    pdf.set_fill_color(*ACCENT)
    pdf.rect(14, y, 3, card_h, "F")

    # Description
    pdf.set_xy(21, y + 4)
    pdf.set_font("Helvetica", size=10)
    pdf.set_text_color(*MID)
    pdf.multi_cell(172, lh, sec["desc"])

    pdf.ln(2)

    # Bullet points
    for bullet in sec["bullets"]:
        bx = 22
        by = pdf.get_y()
        # Filled circle bullet
        pdf.set_fill_color(*ACCENT)
        pdf.ellipse(bx, by + 1.8, 2.2, 2.2, "F")
        pdf.set_xy(bx + 5, by)
        pdf.set_font("Helvetica", size=9.5)
        pdf.set_text_color(*DARK)
        pdf.multi_cell(170, 6.5, bullet)


def main():
    pdf = PDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(14, 14, 14)

    draw_cover(pdf)

    for i, sec in enumerate(SECTIONS):
        draw_section(pdf, sec, is_first=(i == 0))

    out = r"D:\PlanLift\PlanLift\PlanLift_Overview.pdf"
    pdf.output(out)
    print(f"Saved: {out}")


if __name__ == "__main__":
    main()

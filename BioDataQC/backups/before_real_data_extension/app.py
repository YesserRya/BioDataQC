"""Launch from the project root: python -m streamlit run app.py"""

from pathlib import Path

from biodataqc.ui import main


main(Path(__file__).resolve().parent)

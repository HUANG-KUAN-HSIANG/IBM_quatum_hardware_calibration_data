#!/usr/bin/env bash

set -euo pipefail

VENV_DIR=".venv"
PYTHON="${VENV_DIR}/bin/python3"
PIP="${VENV_DIR}/bin/pip"

# Colors for terminal formatting
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

print_help() {
    echo -e "${BLUE}=== IBM Quantum Calibration Manager ===${NC}"
    echo "Usage: ./run.sh [command]"
    echo ""
    echo "Commands:"
    echo "  setup             Create virtual environment and install requirements"
    echo "  download          Download fresh calibration history (requires IBM account)"
    echo "  fetch-coupling    Download qubit coupling maps"
    echo "  analyze           Clean data and compute summary statistics/reports"
    echo "  plot              Generate analysis figures (saved in figures/)"
    echo "  prepare-dashboard Pre-compute data for the interactive dashboard"
    echo "  dashboard         Launch the interactive Plotly Dash dashboard"
    echo "  all               Run setup, analysis, plotting, and dashboard preparation"
    echo "  help              Display this help message"
    echo ""
}

# Verify virtual environment exists before running python scripts
check_venv() {
    if [ ! -d "$VENV_DIR" ]; then
        echo -e "${RED}Error: Virtual environment not found at $VENV_DIR.${NC}"
        echo "Please run './run.sh setup' first."
        exit 1
    fi
}

case "${1:-help}" in
    setup)
        echo -e "${BLUE}Setting up Python virtual environment...${NC}"
        python3 -m venv "$VENV_DIR"
        echo -e "${BLUE}Upgrading pip and installing project requirements...${NC}"
        "$PIP" install --upgrade pip
        if [ -f "requirements.txt" ]; then
            "$PIP" install -r requirements.txt
        else
            echo -e "${RED}Warning: requirements.txt not found!${NC}"
        fi
        echo -e "${GREEN}Setup completed successfully!${NC}"
        ;;
    download)
        check_venv
        echo -e "${BLUE}Downloading calibration history from IBM Quantum...${NC}"
        "$PYTHON" get_ibm.py
        ;;
    fetch-coupling)
        check_venv
        echo -e "${BLUE}Fetching qubit coupling maps...${NC}"
        "$PYTHON" summarize_ibm.py --fetch-coupling
        ;;
    analyze)
        check_venv
        echo -e "${BLUE}Cleaning data and generating summary statistics...${NC}"
        if [ ! -f "ibm_calibration_history.csv" ]; then
            echo -e "${RED}Error: ibm_calibration_history.csv not found.${NC}"
            exit 1
        fi
        COUPLING_ARG=""
        if [ -f "coupling_map.csv" ]; then
            COUPLING_ARG="--coupling coupling_map.csv"
        fi
        "$PYTHON" summarize_ibm.py ibm_calibration_history.csv $COUPLING_ARG
        echo -e "${GREEN}Analysis complete. Output saved in ibm_summary/.${NC}"
        ;;
    plot)
        check_venv
        echo -e "${BLUE}Generating analysis figures...${NC}"
        "$PYTHON" plot_ibm.py
        echo -e "${GREEN}Figures saved in figures/.${NC}"
        ;;
    prepare-dashboard)
        check_venv
        echo -e "${BLUE}Preparing data for the dashboard...${NC}"
        "$PYTHON" prepare_dashboard_data.py
        echo -e "${GREEN}Dashboard data ready in dashboard_data/.${NC}"
        ;;
    dashboard)
        check_venv
        echo -e "${BLUE}Starting interactive dashboard at http://127.0.0.1:8050 ...${NC}"
        echo "Press Ctrl+C to stop."
        "$PYTHON" dashboard.py
        ;;
    all)
        echo -e "${BLUE}Running full pipeline (Setup -> Analyze -> Plot -> Prepare Dashboard)...${NC}"
        "$0" setup
        "$0" analyze
        "$0" plot
        "$0" prepare-dashboard
        echo -e "${GREEN}Full pipeline completed successfully!${NC}"
        ;;
    help|--help|-h)
        print_help
        ;;
    *)
        echo -e "${RED}Unknown command: $1${NC}"
        print_help
        exit 1
        ;;
esac
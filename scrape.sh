#!/bin/bash
# MasterMarket Scraper - Simple execution script
# Usage: ./scrape.sh [store] [products] [options]

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Default values
STORE=""
PRODUCTS=50
RETRY_MODE=""
DEBUG_PRICES=""

# Help function
show_help() {
    echo "MasterMarket Scraper"
    echo ""
    echo "Usage: ./scrape.sh [store] [products] [options]"
    echo ""
    echo "Stores:"
    echo "  aldi, a        - Aldi Ireland"
    echo "  tesco, t       - Tesco Ireland"
    echo "  supervalu, sv  - SuperValu"
    echo "  dunnes, d      - Dunnes Stores"
    echo "  all            - All stores in sequence"
    echo ""
    echo "Options:"
    echo "  -p, --products N    Number of products (default: 50)"
    echo "  -r, --retry         Retry mode (only failed/pending products)"
    echo "  -d, --debug         Enable debug prices logging"
    echo "  -h, --help          Show this help"
    echo ""
    echo "Examples:"
    echo "  ./scrape.sh aldi                    # Scrape 50 Aldi products"
    echo "  ./scrape.sh tesco 100               # Scrape 100 Tesco products"
    echo "  ./scrape.sh supervalu -r            # Retry failed SuperValu products"
    echo "  ./scrape.sh dunnes 20 -r -d         # Dunnes with retry and debug"
    echo "  ./scrape.sh all 30                  # All stores, 30 products each"
    echo ""
}

# Parse store name
parse_store() {
    case "${1,,}" in
        aldi|a)
            echo "Aldi"
            ;;
        tesco|t)
            echo "Tesco"
            ;;
        supervalu|sv|super)
            echo "SuperValu"
            ;;
        dunnes|d|dunnes_stores)
            echo "Dunnes Stores"
            ;;
        all)
            echo "all"
            ;;
        *)
            echo ""
            ;;
    esac
}

# Run scraper for a single store
run_scraper() {
    local store="$1"
    local products="$2"
    local retry="$3"
    local debug="$4"

    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}  Scraping: $store ($products products)${NC}"
    echo -e "${GREEN}========================================${NC}"

    cmd="python3 simple_local_to_prod.py --store \"$store\" --products $products"

    if [ -n "$retry" ]; then
        cmd="$cmd --retry-mode"
        echo -e "${YELLOW}  Mode: Retry (failed/pending only)${NC}"
    fi

    if [ -n "$debug" ]; then
        cmd="$cmd --debug-prices"
        echo -e "${YELLOW}  Debug: Enabled${NC}"
    fi

    echo ""
    eval $cmd

    return $?
}

# No arguments - show help
if [ $# -eq 0 ]; then
    show_help
    exit 0
fi

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            show_help
            exit 0
            ;;
        -p|--products)
            PRODUCTS="$2"
            shift 2
            ;;
        -r|--retry)
            RETRY_MODE="yes"
            shift
            ;;
        -d|--debug)
            DEBUG_PRICES="yes"
            shift
            ;;
        *)
            # Check if it's a store name
            if [ -z "$STORE" ]; then
                STORE=$(parse_store "$1")
                if [ -z "$STORE" ]; then
                    # Maybe it's a number (products)
                    if [[ "$1" =~ ^[0-9]+$ ]]; then
                        PRODUCTS="$1"
                    else
                        echo -e "${RED}Error: Unknown store '$1'${NC}"
                        echo "Use --help for available stores"
                        exit 1
                    fi
                fi
            elif [[ "$1" =~ ^[0-9]+$ ]]; then
                PRODUCTS="$1"
            fi
            shift
            ;;
    esac
done

# Validate store
if [ -z "$STORE" ]; then
    echo -e "${RED}Error: No store specified${NC}"
    show_help
    exit 1
fi

# Run scraping
if [ "$STORE" = "all" ]; then
    echo -e "${GREEN}Running all stores...${NC}"
    echo ""

    for s in "Aldi" "Tesco" "SuperValu" "Dunnes Stores"; do
        run_scraper "$s" "$PRODUCTS" "$RETRY_MODE" "$DEBUG_PRICES"
        echo ""
        if [ "$s" != "Dunnes Stores" ]; then
            echo "Waiting 10s before next store..."
            sleep 10
        fi
    done

    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}  All stores completed!${NC}"
    echo -e "${GREEN}========================================${NC}"
else
    run_scraper "$STORE" "$PRODUCTS" "$RETRY_MODE" "$DEBUG_PRICES"
fi

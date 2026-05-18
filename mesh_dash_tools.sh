#!/bin/bash

# ==============================================================================
# MeshDash Management Toolkit
# v2.3 - Interactive Administration Utility
# ==============================================================================

# --- Styling & Colors ---
ESC_SEQ="\x1b["
COL_RESET=$ESC_SEQ"39;49;00m"
COL_RED=$ESC_SEQ"31;01m"
COL_GREEN=$ESC_SEQ"32;01m"
COL_YELLOW=$ESC_SEQ"33;01m"
COL_BLUE=$ESC_SEQ"34;01m"
COL_MAGENTA=$ESC_SEQ"35;01m"
COL_CYAN=$ESC_SEQ"36;01m"
BOLD=$ESC_SEQ"1m"

# --- Configuration & Paths ---
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
CONFIG_FILE="$SCRIPT_DIR/.mesh-dash_config"
SERVICE_NAME="mesh-dash.service"
BACKUP_DIR="$SCRIPT_DIR/backup"
VENV_PYTHON="$SCRIPT_DIR/mesh-dash_venv/bin/python"

# --- Helper Functions ---

get_config_value() {
    # Reads a key from the config file, returns value or default if missing
    local key=$1
    local default=$2
    if [ -f "$CONFIG_FILE" ]; then
        # Grep key, cut after =, remove carriage returns
        val=$(grep "^$key=" "$CONFIG_FILE" | cut -d'=' -f2 | tr -d '\r')
        if [ -n "$val" ]; then
            echo "$val"
            return
        fi
    fi
    echo "$default"
}

# Dynamically load DB filenames from config
# If config is missing or keys empty, defaults to standard names
DB_FILENAME=$(get_config_value "DB_PATH" "meshtastic_data.db")
NET_DB_FILENAME=$(get_config_value "NETWORK_DB_PATH" "meshtastic_network_data.db")

# Force full path relative to this script script location
DB_FILE="$SCRIPT_DIR/$DB_FILENAME"
NET_DB_FILE="$SCRIPT_DIR/$NET_DB_FILENAME"

get_timestamp() {
    date +"%Y-%m-%d_%H-%M-%S"
}

draw_header() {
    clear
    echo -e "${COL_CYAN}"
cat << "EOF"
 _  _  ____  ____  _  _  ____   __   ____  _  _
( \/ )(  __)/ ___) )( \(    \ / _\ / ___)/ )( \
/ \/ \ ) _) \___ \) __ ( ) D (/    \\___ \) __ (
\_)(_/(____)(____/\_)(_/(____/\_/\_/(____/\_)(_/

EOF
    echo -e "${COL_RESET}"
    echo -e "  ${BOLD}MeshDash Management Toolkit${COL_RESET} | ${COL_BLUE}$SCRIPT_DIR${COL_RESET}"
    echo -e "  $(date)"
    echo -e "${COL_BLUE}======================================================${COL_RESET}"
}

pause() {
    echo ""
    read -p "  Press [Enter] to continue..."
}

# --- Module: Service Management ---

get_service_status_line() {
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        echo -e "  Service Status: ${COL_GREEN}● ACTIVE (Running)${COL_RESET}"
    else
        echo -e "  Service Status: ${COL_RED}● INACTIVE (Stopped)${COL_RESET}"
    fi
    
    if systemctl is-enabled --quiet "$SERVICE_NAME"; then
        echo -e "  Boot Start:     ${COL_GREEN}ENABLED${COL_RESET}"
    else
        echo -e "  Boot Start:     ${COL_RED}DISABLED${COL_RESET}"
    fi
}

menu_service() {
    while true; do
        draw_header
        echo -e "${COL_MAGENTA}  --- Service Management ---${COL_RESET}"
        echo ""
        get_service_status_line
        echo ""
        echo "  1) Start Service"
        echo "  2) Stop Service"
        echo "  3) Restart Service"
        echo "  4) Enable Start on Boot"
        echo "  5) Disable Start on Boot"
        echo "  6) View Live Logs (Press Ctrl+C to exit logs)"
        echo ""
        echo "  0) Back to Main Menu"
        echo ""
        read -p "  Select an option: " choice

        case $choice in
            1) echo ""; sudo systemctl start "$SERVICE_NAME" && echo -e "  ${COL_GREEN}Service Started.${COL_RESET}" ;;
            2) echo ""; sudo systemctl stop "$SERVICE_NAME" && echo -e "  ${COL_RED}Service Stopped.${COL_RESET}" ;;
            3) echo ""; sudo systemctl restart "$SERVICE_NAME" && echo -e "  ${COL_YELLOW}Service Restarted.${COL_RESET}" ;;
            4) echo ""; sudo systemctl enable "$SERVICE_NAME" && echo -e "  ${COL_GREEN}Enabled on Boot.${COL_RESET}" ;;
            5) echo ""; sudo systemctl disable "$SERVICE_NAME" && echo -e "  ${COL_RED}Disabled on Boot.${COL_RESET}" ;;
            6) 
                echo -e "  ${COL_CYAN}Loading logs... Press Ctrl+C to return.${COL_RESET}"
                sudo journalctl -u "$SERVICE_NAME" -f 
                ;;
            0) return ;;
            *) echo -e "  ${COL_RED}Invalid option.${COL_RESET}" ;;
        esac
        pause
    done
}

# --- Module: Database Management ---

menu_database() {
    while true; do
        draw_header
        echo -e "${COL_MAGENTA}  --- Database Management ---${COL_RESET}"
        echo -e "  Primary DB Path:  $DB_FILE"
        echo -e "  Network DB Path:  $NET_DB_FILE"
        
        if [ -f "$DB_FILE" ]; then
            DB_SIZE=$(du -h "$DB_FILE" | cut -f1)
            echo -e "  Main DB Size:     ${COL_GREEN}$DB_SIZE${COL_RESET}"
        else
            echo -e "  Main DB Size:     ${COL_RED}Not Found${COL_RESET}"
        fi
        echo -e "  Backup Path:      $BACKUP_DIR/sql/"
        echo ""
        echo "  1) Backup Databases"
        echo "  2) Restore: Most Recent Backup"
        echo "  3) Restore: Select Specific File"
        echo ""
        echo "  0) Back to Main Menu"
        echo ""
        read -p "  Select an option: " choice

        mkdir -p "$BACKUP_DIR/sql"

        case $choice in
            1)
                TS=$(get_timestamp)
                echo ""
                if [ -f "$DB_FILE" ]; then
                    cp "$DB_FILE" "$BACKUP_DIR/sql/${TS}_${DB_FILENAME}"
                    echo -e "  ${COL_GREEN}✔ Backed up Data DB${COL_RESET} -> ${TS}_${DB_FILENAME}"
                else
                    echo -e "  ${COL_RED}✘ Primary DB file not found at expected path${COL_RESET}"
                fi
                
                if [ -f "$NET_DB_FILE" ]; then
                    cp "$NET_DB_FILE" "$BACKUP_DIR/sql/${TS}_${NET_DB_FILENAME}"
                    echo -e "  ${COL_GREEN}✔ Backed up Network DB${COL_RESET} -> ${TS}_${NET_DB_FILENAME}"
                else
                    echo -e "  ${COL_YELLOW}⚠ Network DB not found (skipping)${COL_RESET}"
                fi
                ;;
            2)
                # Find latest by looking for the main DB filename pattern
                LATEST_MAIN=$(ls -t "$BACKUP_DIR/sql/"*"_${DB_FILENAME}" 2>/dev/null | head -n1)
                if [ -z "$LATEST_MAIN" ]; then
                    echo -e "  ${COL_RED}No backups found.${COL_RESET}"
                else
                    TS_PREFIX=$(basename "$LATEST_MAIN" | sed "s/_${DB_FILENAME}//")
                    LATEST_NET="$BACKUP_DIR/sql/${TS_PREFIX}_${NET_DB_FILENAME}"
                    
                    echo -e "  Found Timestamp: ${COL_CYAN}${TS_PREFIX}${COL_RESET}"
                    echo -e "  Restoring:"
                    echo -e "   - $(basename "$LATEST_MAIN")"
                    [ -f "$LATEST_NET" ] && echo -e "   - $(basename "$LATEST_NET")"
                    
                    read -p "  Overwrite current data? (y/n): " confirm
                    if [[ $confirm == "y" ]]; then
                        sudo systemctl stop "$SERVICE_NAME" 2>/dev/null
                        
                        cp "$LATEST_MAIN" "$DB_FILE"
                        echo -e "  ${COL_GREEN}✔ Restored Main DB${COL_RESET}"
                        
                        if [ -f "$LATEST_NET" ]; then
                            cp "$LATEST_NET" "$NET_DB_FILE"
                            echo -e "  ${COL_GREEN}✔ Restored Network DB${COL_RESET}"
                        fi
                        echo -e "  ${COL_GREEN}Complete.${COL_RESET} Please restart service."
                    fi
                fi
                ;;
            3)
                echo ""
                echo -e "  ${COL_CYAN}Available Backups (by Main DB):${COL_RESET}"
                # Create an array of Main DB backup files
                mapfile -t backups < <(ls -1 "$BACKUP_DIR/sql/"*"_${DB_FILENAME}" 2>/dev/null | sort -r)
                
                if [ ${#backups[@]} -eq 0 ]; then
                    echo -e "  ${COL_RED}No backups found.${COL_RESET}"
                else
                    i=1
                    for backup in "${backups[@]}"; do
                        # Extract just the timestamp for cleaner display
                        fname=$(basename "$backup")
                        ts=${fname%"_$DB_FILENAME"}
                        echo "  $i) $ts"
                        ((i++))
                    done
                    echo ""
                    read -p "  Enter number to restore (0 to cancel): " num
                    
                    if [[ "$num" =~ ^[0-9]+$ ]] && [ "$num" -gt 0 ] && [ "$num" -le "${#backups[@]}" ]; then
                        SELECTED_MAIN="${backups[$((num-1))]}"
                        TS_PREFIX=$(basename "$SELECTED_MAIN" | sed "s/_${DB_FILENAME}//")
                        SELECTED_NET="$BACKUP_DIR/sql/${TS_PREFIX}_${NET_DB_FILENAME}"
                        
                        echo -e "  Restoring Backup Set: ${COL_CYAN}${TS_PREFIX}${COL_RESET}..."
                        sudo systemctl stop "$SERVICE_NAME" 2>/dev/null
                        
                        cp "$SELECTED_MAIN" "$DB_FILE"
                        echo -e "  ${COL_GREEN}✔ Restored Main DB${COL_RESET}"
                        
                        if [ -f "$SELECTED_NET" ]; then
                            cp "$SELECTED_NET" "$NET_DB_FILE"
                            echo -e "  ${COL_GREEN}✔ Restored Network DB${COL_RESET}"
                        else
                            echo -e "  ${COL_YELLOW}⚠ No matching Network DB found for this timestamp.${COL_RESET}"
                        fi
                        
                        echo -e "  ${COL_GREEN}Done.${COL_RESET} Please restart service."
                    fi
                fi
                ;;
            0) return ;;
            *) echo -e "  ${COL_RED}Invalid option.${COL_RESET}" ;;
        esac
        pause
    done
}

# --- Module: Configuration Management ---

menu_config() {
    while true; do
        draw_header
        echo -e "${COL_MAGENTA}  --- Configuration Management ---${COL_RESET}"
        echo -e "  Config File:    $CONFIG_FILE"
        echo -e "  Backup Path:    $BACKUP_DIR/config/"
        echo ""
        echo "  1) Backup Configuration"
        echo "  2) Restore Configuration"
        echo "  3) View Current Config"
        echo "  4) Edit Config (Safe Mode)"
        echo ""
        echo "  0) Back to Main Menu"
        echo ""
        read -p "  Select an option: " choice

        mkdir -p "$BACKUP_DIR/config"

        case $choice in
            1)
                TS=$(get_timestamp)
                cp "$CONFIG_FILE" "$BACKUP_DIR/config/${TS}_config"
                echo -e "  ${COL_GREEN}Backup created: ${TS}_config${COL_RESET}"
                ;;
            2)
                echo ""
                echo -e "  ${COL_CYAN}Available Configs:${COL_RESET}"
                mapfile -t configs < <(ls -1 "$BACKUP_DIR/config/"* 2>/dev/null | sort -r)
                
                if [ ${#configs[@]} -eq 0 ]; then
                    echo -e "  ${COL_RED}No backups found.${COL_RESET}"
                else
                    i=1
                    for cfg in "${configs[@]}"; do
                        echo "  $i) $(basename "$cfg")"
                        ((i++))
                    done
                    echo ""
                    read -p "  Enter number to restore (0 to cancel): " num
                    if [[ "$num" =~ ^[0-9]+$ ]] && [ "$num" -gt 0 ] && [ "$num" -le "${#configs[@]}" ]; then
                        FILE="${configs[$((num-1))]}"
                        cp "$FILE" "$CONFIG_FILE"
                        echo -e "  ${COL_GREEN}Configuration restored.${COL_RESET} Restart service to apply."
                    fi
                fi
                ;;
            3)
                echo ""
                echo -e "${COL_BLUE}--- START CONFIG ---${COL_RESET}"
                cat "$CONFIG_FILE"
                echo -e "${COL_BLUE}--- END CONFIG ---${COL_RESET}"
                ;;
            4)
                if command -v nano &> /dev/null; then
                    nano "$CONFIG_FILE"
                else
                    vi "$CONFIG_FILE"
                fi
                ;;
            0) return ;;
            *) echo -e "  ${COL_RED}Invalid option.${COL_RESET}" ;;
        esac
        pause
    done
}

# --- Module: Admin & Maintenance ---

menu_admin() {
    while true; do
        draw_header
        echo -e "${COL_MAGENTA}  --- Admin & Maintenance ---${COL_RESET}"
        echo ""
        echo "  1) Fix Directory Permissions (chown to $(whoami))"
        echo "  2) Clear Stuck Update Flags"
        echo "  3) Show Disk Usage"
        echo ""
        echo "  0) Back to Main Menu"
        echo ""
        read -p "  Select an option: " choice

        case $choice in
            1) 
                echo -e "  Setting ownership of $SCRIPT_DIR to $(whoami)..."
                sudo chown -R "$(whoami):$(id -gn)" "$SCRIPT_DIR"
                echo -e "  ${COL_GREEN}Done.${COL_RESET}"
                ;;
            2)
                rm -f "$SCRIPT_DIR/.update_ready" "$SCRIPT_DIR/update.zip" "$SCRIPT_DIR/.update_temp_extract"
                echo -e "  ${COL_GREEN}Update flags and temporary files cleared.${COL_RESET}"
                ;;
            3)
                echo ""
                echo -e "  ${COL_CYAN}Disk Usage:${COL_RESET}"
                du -sh "$SCRIPT_DIR"
                echo ""
                echo -e "  ${COL_CYAN}Largest Files:${COL_RESET}"
                find "$SCRIPT_DIR" -type f -exec du -h {} + | sort -rh | head -n 5
                ;;
            0) return ;;
            *) echo -e "  ${COL_RED}Invalid option.${COL_RESET}" ;;
        esac
        pause
    done
}

# --- Main Logic ---

while true; do
    draw_header
    echo -e "  System: ${BOLD}$(hostname)${COL_RESET}"
    get_service_status_line
    echo -e "${COL_BLUE}------------------------------------------------------${COL_RESET}"
    echo "  1) Service Control (Start/Stop/Logs)"
    echo "  2) Database Tools (Backup/Restore)"
    echo "  3) Configuration Tools (Edit/Backup)"
    echo "  4) Admin & Maintenance"
    echo ""
    echo "  0) Exit"
    echo ""
    read -p "  Select an option: " main_choice

    case $main_choice in
        1) menu_service ;;
        2) menu_database ;;
        3) menu_config ;;
        4) menu_admin ;;
        0) 
            clear
            echo "Exiting MeshDash Tools."
            exit 0 
            ;;
        *) echo -e "  ${COL_RED}Invalid option.${COL_RESET}"; pause ;;
    esac
done
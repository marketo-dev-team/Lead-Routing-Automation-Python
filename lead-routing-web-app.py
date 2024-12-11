from flask import Flask, render_template, request, jsonify, send_file
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import pandas as pd
import io
import os
import logging
from datetime import datetime

# Set up logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

current_dir = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(current_dir, 'templates'))

SERVICE_ACCOUNT_FILE = os.path.join(current_dir, 'lead-routing-automation-08f6c51bf9a8.json')
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

def get_credentials():
    try:
        credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        if credentials.expired:
            credentials.refresh(Request())
        logging.info("Credentials obtained successfully")
        return credentials
    except Exception as e:
        logging.error(f"Error getting credentials: {str(e)}")
        return None

def remove_duplicates_and_log(spreadsheet_id):
    logging.info(f"Starting remove_duplicates_and_log for spreadsheet: {spreadsheet_id}")
    credentials = get_credentials()
    if not credentials:
        return {'error': 'Failed to obtain credentials'}
    
    try:
        service = build('sheets', 'v4', credentials=credentials)
        logging.info("Google Sheets service built successfully")
        
        # Get the main sheet data
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range='Sheet1!A:ZZ'
        ).execute()
        
        logging.info("Successfully retrieved spreadsheet data")
        
        values = result.get('values', [])
        if not values:
            logging.warning("No data found in the spreadsheet")
            return {'error': 'No data found.'}
        
        headers = values[0]
        df = pd.DataFrame(values[1:], columns=headers)
        
        if 'Custom Field' not in df.columns:
            logging.error("Custom Field column not found in the spreadsheet")
            return {'error': 'Custom Field column not found'}
        
        initial_row_count = len(df)
        logging.info(f"Initial row count: {initial_row_count}")
        
        # Store deleted rows for logging
        deleted_rows = pd.DataFrame(columns=df.columns)
        custom_field_groups = df.groupby('Custom Field', dropna=False)
        rows_to_keep = set()
        
        for custom_field_value, group in custom_field_groups:
            if pd.isna(custom_field_value) or str(custom_field_value).strip() == '':
                # Keep rows with empty Custom Field values
                rows_to_keep.update(group.index)
                continue
            
            if len(group) == 1:
                # Keep unique Custom Field values
                rows_to_keep.update(group.index)
                continue
                
            # Check if all rows in the group are identical
            group_without_index = group.copy()
            are_rows_identical = group_without_index.duplicated(keep=False).all()
            
            if are_rows_identical:
                # Keep only the first occurrence for identical rows
                first_index = group.index[0]
                rows_to_keep.add(first_index)
                # Add other occurrences to deleted_rows
                deleted_rows = pd.concat([deleted_rows, group.iloc[1:]])
            else:
                # For non-identical rows with same Custom Field, delete all rows including first occurrence
                deleted_rows = pd.concat([deleted_rows, group])
        
        df_cleaned = df.iloc[list(sorted(rows_to_keep))]
        logging.info(f"Rows after removing duplicates: {len(df_cleaned)}")
        
        # Update the main sheet with cleaned data
        values = [df_cleaned.columns.tolist()] + df_cleaned.values.tolist()
        body = {
            'values': values
        }
        
        service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range='Sheet1!A:ZZ'
        ).execute()
        logging.info("Cleared original spreadsheet data")
        
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range='Sheet1!A1',
            valueInputOption='RAW',
            body=body
        ).execute()
        
        # Update the Deleted Duplicates Log sheet with complete deleted rows
        if not deleted_rows.empty:
            deleted_values = [deleted_rows.columns.tolist()] + deleted_rows.values.tolist()
            
            service.spreadsheets().values().clear(
                spreadsheetId=spreadsheet_id,
                range='Deleted Duplicates Log!A:ZZ'
            ).execute()
            
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range='Deleted Duplicates Log!A1',
                valueInputOption='RAW',
                body={'values': deleted_values}
            ).execute()
            
            logging.info(f"Updated duplicate log with {len(deleted_rows)} deleted rows")
        
        return {
            'initial_row_count': initial_row_count,
            'final_row_count': len(df_cleaned),
            'duplicates_removed': initial_row_count - len(df_cleaned)
        }
        
    except HttpError as e:
        logging.error(f"HTTP error occurred: {e.resp.status} {e.resp.reason}")
        return {'error': f'HTTP error occurred: {e.resp.status} {e.resp.reason}'}
    except Exception as e:
        logging.error(f"Unexpected error: {str(e)}")
        return {'error': str(e)}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/remove-duplicates', methods=['POST'])
def remove_duplicates():
    spreadsheet_id = request.json.get('spreadsheet_id')
    if not spreadsheet_id:
        logging.error("No spreadsheet ID provided")
        return jsonify({'error': 'No spreadsheet ID provided'}), 400
    
    logging.info(f"Removing duplicates for spreadsheet: {spreadsheet_id}")
    result = remove_duplicates_and_log(spreadsheet_id)
    return jsonify(result)

@app.route('/download-csv', methods=['POST'])
def download_csv():
    spreadsheet_id = request.json.get('spreadsheet_id')
    territory = request.json.get('territory')
    round_robin_name = request.json.get('round_robin_name')
    custom_field = request.json.get('custom_field')
    
    if not spreadsheet_id:
        logging.error("No spreadsheet ID provided for CSV download")
        return jsonify({'error': 'No spreadsheet ID provided'}), 400
    
    logging.info(f"Downloading CSV for spreadsheet: {spreadsheet_id}")
    credentials = get_credentials()
    if not credentials:
        logging.error("Failed to obtain credentials for CSV download")
        return jsonify({'error': 'Failed to obtain credentials'}), 500
    
    try:
        service = build('sheets', 'v4', credentials=credentials)
        
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range='Sheet1!A:ZZ'
        ).execute()
        
        values = result.get('values', [])
        if not values:
            logging.warning("No data found for CSV download")
            return jsonify({'error': 'No data found.'}), 404
        
        headers = values[0]
        df = pd.DataFrame(values[1:], columns=headers)
        
        # Remove 'Owner Name' column if it exists
        if 'Owner Name' in df.columns:
            df = df.drop('Owner Name', axis=1)
            logging.info("Removed 'Owner Name' column")
        
        # Create a new row with the additional information
        new_row = pd.DataFrame([['' for _ in range(len(df.columns))]], columns=df.columns)
        new_row['Territory'] = territory
        new_row['Round Robin Name'] = round_robin_name
        new_row['Custom Field'] = custom_field
        
        # Concatenate the original DataFrame with the new row
        df = pd.concat([df, new_row], ignore_index=True)
        
        logging.info(f"Final columns in DataFrame: {df.columns.tolist()}")
        
        output = io.StringIO()
        df.to_csv(output, index=False)
        output.seek(0)
        
        logging.info("CSV file created successfully")
        return send_file(
            io.BytesIO(output.getvalue().encode()),
            mimetype='text/csv',
            as_attachment=True,
            download_name='cleaned_data.csv'
        )
    
    except HttpError as e:
        logging.error(f"HTTP error during CSV download: {e.resp.status} {e.resp.reason}")
        return jsonify({'error': f'HTTP error occurred: {e.resp.status} {e.resp.reason}'}), 500
    except Exception as e:
        logging.error(f"Unexpected error during CSV download: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)
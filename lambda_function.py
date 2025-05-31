import json
import csv
import io
import os
import uuid
import boto3
from botocore.exceptions import ClientError

# Initialize AWS clients
s3_client = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')
ses_client = boto3.client('ses')

# Get environment variables
# Set these in your Lambda function's configuration under "Environment variables"
DYNAMODB_TABLE_NAME = os.environ.get('DYNAMODB_TABLE_NAME', 'xyzData') # Default placeholder
MANAGEMENT_EMAIL = os.environ.get('MANAGEMENT_EMAIL', 'lasinidissanayakeaws@gmail.com') # Default placeholder
SENDER_EMAIL = os.environ.get('SENDER_EMAIL', 'lasinidissanayakeaws@gmail.com') # Default placeholder, MUST be SES verified!

def send_email(recipient_email, subject, body_text, body_html=None):
    """
    Sends an email using AWS SES.
    """
    # Check if SENDER_EMAIL is verified (essential for SES)
    if SENDER_EMAIL == 'lasinidissanayakeaws@gmail.com' and not os.environ.get('SENDER_EMAIL'):
        print("Warning: SENDER_EMAIL is default. Ensure it's set as an environment variable and SES verified.")
        return # Do not attempt to send if sender is not properly configured

    try:
        response = ses_client.send_email(
            Destination={
                'ToAddresses': [
                    recipient_email,
                ],
            },
            Message={
                'Body': {
                    'Html': {
                        'Charset': 'UTF-8',
                        'Data': body_html if body_html else body_text,
                    },
                    'Text': {
                        'Charset': 'UTF-8',
                        'Data': body_text,
                    },
                },
                'Subject': {
                    'Charset': 'UTF-8',
                    'Data': subject,
                },
            },
            Source=SENDER_EMAIL,
        )
        print(f"Email sent to {recipient_email}! Message ID: {response['MessageId']}")
    except ClientError as e:
        print(f"Error sending email to {recipient_email}: {e.response['Error']['Message']}")
        # Log the error but don't re-raise to allow the Lambda to complete (unless it's critical to retry)
    except Exception as e:
        print(f"An unexpected error occurred while sending email to {recipient_email}: {e}")


def lambda_handler(event, context):
    """
    AWS Lambda handler function.
    Triggered by SQS messages, which contain S3 object creation events from EventBridge.
    """
    print(f"Received SQS event: {json.dumps(event)}")

    # SQS events can contain multiple records (messages) in one batch
    for record in event['Records']:
        try:
            # Each record's 'body' contains the actual message, which should be the S3 event
            message_body = json.loads(record['body'])
            print(f"Parsed SQS message body: {json.dumps(message_body)}")

            # --- MODIFICATION START ---
            # The S3 event comes from EventBridge, so it's a direct JSON object, not nested within 'Records' inside message_body.
            # The 'source' and 'detail-type' are top-level keys in the message_body.
            event_source = message_body.get('source')
            detail_type = message_body.get('detail-type')

            # Check if it's an S3 Object Created event from the 'aws.s3' source
            if not (event_source == 'aws.s3' and detail_type == 'Object Created'):
                print(f"Skipping non-S3 'Object Created' event or malformed event: {json.dumps(message_body)}")
                continue # Move to the next SQS record

            # Extract S3 details from the 'detail' key of the EventBridge event
            s3_detail = message_body.get('detail', {})
            bucket_name = s3_detail.get('bucket', {}).get('name')
            file_key = s3_detail.get('object', {}).get('key')
            file_size = s3_detail.get('object', {}).get('size')
            event_time = message_body.get('time') # EventBridge uses 'time' for the event timestamp

            # The 'requester' field in the 'detail' object of the EventBridge event often contains
            # the principal ID of the entity that performed the S3 action.
            # This is typically the AWS Account ID if uploaded via root, or a more specific IAM user/role ARN.
            uploader_principal_id = s3_detail.get('requester', 'unknown')
            user_id = uploader_principal_id # Using requester as user_id for now.
            # --- MODIFICATION END ---

            # Determine uploader's email for acknowledgment.
            # Best approach: Uploader's email passed as S3 object metadata during upload.
            # Ensure your frontend passes a custom header like 'x-amz-meta-uploader-email'.
            uploader_email = None
            try:
                head_response = s3_client.head_object(Bucket=bucket_name, Key=file_key)
                if 'Metadata' in head_response and 'uploader-email' in head_response['Metadata']:
                    uploader_email = head_response['Metadata']['uploader-email']
                    print(f"Retrieved uploader email from S3 metadata: {uploader_email}")
                else:
                    print("Warning: 'uploader-email' metadata not found on S3 object.")
            except ClientError as e:
                print(f"Error retrieving S3 object metadata for {file_key}: {e.response['Error']['Message']}")

            # Fallback for uploader_email if not found in metadata.
            # This is a weak fallback; a robust solution would use Cognito user data or a predefined list.
            if not uploader_email:
                # If user_id is an email (e.g., from Cognito), use it. Otherwise, create a placeholder.
                if '@' in user_id and '.' in user_id: # Simple check for email format
                    uploader_email = user_id
                else:
                    # Still a placeholder, but now we're explicitly acknowledging it's a fallback
                    uploader_email = f"{user_id.replace(':', '_').lower()}@example.com"
                print(f"Falling back to uploader email: {uploader_email}")

        except (KeyError, json.JSONDecodeError) as e:
            print(f"Error parsing SQS message or S3 event details. Skipping record: {record}. Error: {e}")
            # If the SQS message cannot be parsed, you might want to send it to a DLQ
            # by allowing the Lambda to fail for this record, or handle it here if it's
            # not critical to retry. For now, we'll log and move to next record.
            continue # Move to the next SQS record in the batch
        except Exception as e:
            print(f"An unexpected error occurred during initial event processing: {e}")
            continue # Move to the next SQS record in the batch


        print(f"Processing file: {file_key} from bucket: {bucket_name} by user: {user_id}")

        # --- File Type Check ---
        # Add .xlsx support if needed, requires pandas Lambda layer
        if not file_key.lower().endswith(('.csv', '.xlsx')):
            print(f"Skipping file {file_key}: Not a supported file type (CSV or XLSX).")
            send_email(
                recipient_email=uploader_email,
                subject=f"File Upload Failed: {file_key}",
                body_text=f"Dear Uploader,\n\nYour file '{file_key}' was uploaded to S3, but it does not appear to be a CSV or XLSX file. Only these formats are supported for processing.\n\nPlease upload a valid file.\n\nRegards,\nXYZ Logistics Automation Team",
                body_html=f"<p>Dear Uploader,</p><p>Your file '<b>{file_key}</b>' was uploaded to S3, but it does not appear to be a CSV or XLSX file. Only these formats are supported for processing.</p><p>Please upload a valid file.</p><p>Regards,<br/>XYZ Logistics Automation Team</p>"
            )
            continue # Move to the next SQS record

        try:
            # Get the file from S3
            response = s3_client.get_object(Bucket=bucket_name, Key=file_key)
            file_content_bytes = response['Body'].read()

            processed_records_count = 0
            
            # --- Process CSV or XLSX based on file extension ---
            if file_key.lower().endswith('.csv'):
                csv_file = io.StringIO(file_content_bytes.decode('utf-8'))
                reader = csv.DictReader(csv_file)
                # Ensure all column names are stripped of whitespace
                header = [h.strip() for h in reader.fieldnames]
                data_rows = [{k.strip(): v.strip() if v is not None else '' for k, v in row.items()} for row in reader]

            elif file_key.lower().endswith('.xlsx'):
                # This part requires the 'pandas' library as a Lambda Layer
                # from io import BytesIO
                # df = pd.read_excel(BytesIO(file_content_bytes))
                # # Convert DataFrame to a list of dictionaries, handling NaNs
                # data_rows = df.astype(str).replace({'nan': ''}).to_dict(orient='records')
                
                # Placeholder if pandas layer is not set up:
                print(f"Warning: XLSX file {file_key} detected, but pandas/openpyxl is required for processing. Skipping content processing.")
                send_email(
                    recipient_email=uploader_email,
                    subject=f"File Processing Warning: {file_key}",
                    body_text=f"Dear Uploader,\n\nYour XLSX file '{file_key}' was received, but the processing system for XLSX files is not fully configured (requires a Lambda Layer). The file will be skipped for now.\n\nRegards,\nXYZ Logistics Automation Team",
                    body_html=f"<p>Dear Uploader,</p><p>Your XLSX file '<b>{file_key}</b>' was received, but the processing system for XLSX files is not fully configured (requires a Lambda Layer). The file will be skipped for now.</p><p>Regards,<br/>XYZ Logistics Automation Team</p>"
                )
                continue # Skip to the next SQS record
            else:
                # This case should ideally be caught by the file type check above, but for safety:
                print(f"Unsupported file type for processing: {file_key}")
                continue # Skip to the next SQS record


            # Get DynamoDB table
            table = dynamodb.Table(DYNAMODB_TABLE_NAME)
            
            # Iterate over each row and store in DynamoDB
            for row_data in data_rows:
                # Generate a unique xyzId for each record
                xyz_id = str(uuid.uuid4())

                # Prepare item for DynamoDB
                item = {
                    'xyzid': xyz_id,
                    'userId': user_id, # The ID of the user who uploaded the file
                    'uploadFileName': file_key,
                    'uploadTime': event_time,
                }
                
                # Add all CSV/Excel columns to the item
                # Ensure values are strings or numbers, and handle potential empty values
                for key, value in row_data.items():
                    # DynamoDB does not allow empty strings for non-key attributes by default
                    # unless a specific DynamoDB setting is enabled (empty string as NULL).
                    # Converting empty strings to None and stripping whitespace.
                    processed_value = value.strip() if isinstance(value, str) and value.strip() != '' else None
                    if processed_value is not None:
                        item[key] = processed_value
                
                try:
                    # Store data in DynamoDB
                    table.put_item(Item=item)
                    processed_records_count += 1
                    print(f"Stored record with xyzId: {xyz_id} for user: {user_id}")
                except ClientError as e:
                    print(f"Error putting item to DynamoDB (xyzId: {xyz_id}): {e.response['Error']['Message']}")
                    # Decide if you want to stop processing the file or continue with next record
                    # For now, it will log the error and continue.
                    # If this is a critical error, you might want to re-raise and let SQS handle retries.
                    # raise e # Uncomment to re-raise and trigger SQS retry mechanism

            print(f"Successfully processed {processed_records_count} records from {file_key}")

            # Send email to management
            management_subject = f"New File Upload Notification: {file_key}"
            management_body_text = (
                f"Dear Management,\n\nA new file '{file_key}' (size: {file_size} bytes) was uploaded by user '{user_id}' "
                f"at {event_time}. It contained {processed_records_count} records.\n\n"
                f"Data has been extracted and stored in the '{DYNAMODB_TABLE_NAME}' DynamoDB table.\n\n"
                f"Regards,\nXYZ Logistics Automation System"
            )
            management_body_html = (
                f"<p>Dear Management,</p>"
                f"<p>A new file '<b>{file_key}</b>' (size: {file_size} bytes) was uploaded by user '<b>{user_id}</b>' "
                f"at {event_time}. It contained {processed_records_count} records.</p>"
                f"<p>Data has been extracted and stored in the '<b>{DYNAMODB_TABLE_NAME}</b>' DynamoDB table.</p>"
                f"<p>Regards,<br/>XYZ Logistics Automation System</p>"
            )
            send_email(MANAGEMENT_EMAIL, management_subject, management_body_text, management_body_html)

            # Send acknowledgment email to uploader
            uploader_subject = f"File Upload Acknowledgment: {file_key}"
            uploader_body_text = (
                f"Dear Uploader,\n\nWe have successfully received and processed your file "
                f"'{file_key}' (size: {file_size} bytes) uploaded at {event_time}. "
                f"It contained {processed_records_count} records.\n\n"
                f"Thank you for your submission.\n\n"
                f"Regards,\nXYZ Logistics Automation Team"
            )
            uploader_body_html = (
                f"<p>Dear Uploader,</p>"
                f"<p>We have successfully received and processed your file "
                f"'<b>{file_key}</b>' (size: {file_size} bytes) uploaded at {event_time}. "
                f"It contained {processed_records_count} records.</p>"
                f"<p>Thank you for your submission.</p>"
                f"<p>Regards,<br/>XYZ Logistics Automation Team</p>"
            )
            send_email(uploader_email, uploader_subject, uploader_body_text, uploader_body_html)

        except ClientError as e:
            error_message = f"AWS Client Error processing {file_key}: {e.response['Error']['Message']}"
            print(error_message)
            send_email(
                recipient_email=uploader_email,
                subject=f"File Processing Failed: {file_key}",
                body_text=f"Dear Uploader,\n\nThere was an error processing your file '{file_key}': {error_message}\n\nPlease try again or contact support.\n\nRegards,\nXYZ Logistics Automation Team",
                body_html=f"<p>Dear Uploader,</p><p>There was an error processing your file '<b>{file_key}</b>': {error_message}</p><p>Please try again or contact support.</p><p>Regards,<br/>XYZ Logistics Automation Team</p>"
            )
            # Re-raise the exception to signal SQS that this message failed and needs a retry
            raise e
        except Exception as e:
            error_message = f"An unexpected error occurred while processing {file_key}: {str(e)}"
            print(error_message)
            send_email(
                recipient_email=uploader_email,
                subject=f"File Processing Failed: {file_key}",
                body_text=f"Dear Uploader,\n\nThere was an unexpected error processing your file '{file_key}': {error_message}\n\nPlease try again or contact support.\n\nRegards,\nXYZ Logistics Automation Team",
                body_html=f"<p>Dear Uploader,</p><p>There was an unexpected error processing your file '<b>{file_key}</b>': {error_message}</p><p>Please try again or contact support.</p><p>Regards,<br/>XYZ Logistics Automation Team</p>"
            )
            # Re-raise the exception to signal SQS that this message failed and needs a retry
            raise e

    return {
        'statusCode': 200,
        'body': json.dumps('All SQS messages in batch processed (or attempted to process)!')
    }
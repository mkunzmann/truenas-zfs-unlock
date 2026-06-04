#!/bin/sh
set -e

# Configure global curl flags
curl_flags="--silent --show-error --connect-timeout 3"
if [ "${SKIP_CERT_VERIFY}" = "true" ]; then
  curl_flags="${curl_flags} --insecure"
fi

load_secrets() {
  # Load all docker secrets into this scripts env. This will override any matching global vars
  for var in $(env | grep '^FILE__'); do
    var_name=$(echo "${var}" | cut -d= -f1)
    var_value=$(echo "${var}" | cut -d= -f2)

    if [ -f "${var_value}" ]; then
      # Read the file contents and store them in a new variable
      new_var_name="${var_name#FILE__}"  # Remove 'FILE__' prefix
      secret="$(cat "${var_value}")"

      # Set the new variable with the secret value
      export "${new_var_name}=${secret}"
    else
      echo "Secret file does not exist: ${var_value}"
      exit 1
    fi
  done
}

load_api_token() {
  # Set api token from env
  if [ -n "${TRUENAS_API_KEY}" ]; then
      api_token="${TRUENAS_API_KEY}"
  else
      echo "'TRUENAS_API_KEY' has not been set as env var or docker secret"
      exit 1
  fi
}

is_locked() {
  dataset_path="$1"

  # Check if dataset is locked or not
  curl_stdout_file="$(mktemp)"
  curl_stderr_file="$(mktemp)"
  # Ignore shellchecks recommendation to double quote "$curl_flags"
  # shellcheck disable=SC2086
  http_code="$(
    curl ${curl_flags} -X 'GET' \
    "https://${TRUENAS_HOST}/api/v2.0/pool/dataset?id=${dataset_path}" \
    -H 'accept: */*' \
    -H "Authorization: Bearer ${api_token}" \
    --output "${curl_stdout_file}" \
    --write-out "%{http_code}" \
    2>"${curl_stderr_file}"
  )"

  # Handle non 200 code
  if [ "${http_code}" -ne 200 ]; then
    echo "Error: curl received a ${http_code} code"
    printf "STDERR: %s\n" "$(cat "${curl_stderr_file}")"
    printf "STDOUT: %s\n" "$(cat "${curl_stdout_file}")"
    rm "${curl_stderr_file}"
    rm "${curl_stdout_file}"
    return 1
  fi

  # Extract lock status of dataset from curl response and cleanup temp files.
  locked="$(jq '.[].locked' "${curl_stdout_file}")"
  rm "${curl_stderr_file}"
  rm "${curl_stdout_file}"

  # Handle invalid json response (Happens even with a 200)
  if [ -z "${locked}" ]; then
    echo "Invalid response from api"
    return 1
  fi
  # Return 0 for locked, else 1
  if [ "${locked}" = 'true' ]; then
    return 0
  elif [ "${locked}" = 'false' ]; then
    echo "${dataset_path} is already unlocked"
    return 1
  else
    echo "Error: got an unexpected value '${locked}' for ${dataset_path} 'locked' status"
  fi
}

unlock (){
  dataset_path="$1"
  passphrase="$2"

  json=$(jq --null-input \
    --arg dataset_path "${dataset_path}" \
    --arg passphrase "${passphrase}" \
    '
      {
        "id": $dataset_path,
        "options": {
          "key_file": false,
          "recursive": false,
          "force": true,
          "toggle_attachments": true,
          "datasets": [
            {
              "name": $dataset_path,
              "passphrase": $passphrase
            }
          ]
        }
      }
    '
  )

  curl_stdout_file="$(mktemp)"
  curl_stderr_file="$(mktemp)"
  # Ignore shellchecks recommendation to double quote "$curl_flags"
  # shellcheck disable=SC2086
  http_code="$(
    curl ${curl_flags} -X 'POST' \
    "https://${TRUENAS_HOST}/api/v2.0/pool/dataset/unlock" \
    -H 'accept: */*' \
    -H "Authorization: Bearer ${api_token}" \
    -H 'Content-Type: application/json' \
    -d "${json}" \
    --output "${curl_stdout_file}" \
    --write-out "%{http_code}" \
    2>"${curl_stderr_file}"
  )"

  # Log non-200 response and cleanup
  if [ "${http_code}" -ne 200 ]; then
    echo "Error: curl received a ${http_code} code"
    printf "STDERR: %s\n" "$(cat "${curl_stderr_file}")"
    printf "STDOUT: %s\n" "$(cat "${curl_stdout_file}")"
  else
    echo "Got a 200 code in response to unlocking request. Does not imply success unfortunately"
    echo "Run again to get the 'locked' status for ${dataset_path}"
  fi
  rm "${curl_stderr_file}"
  rm "${curl_stdout_file}"
}

# Unlock script start point
echo "Starting unlock script"
load_secrets
load_api_token

# Verify Necessary environment variables exist
if [ -z "${TRUENAS_HOST}" ]; then
  echo "Error: Missing 'TRUENAS_HOST' environment variable"
  exit 1
fi

if [ -z "${DATASETS_FILE}" ]; then
  echo "Error: Missing 'DATASETS_FILE' environment variable"
  exit 1
fi

if [ ! -f "${DATASETS_FILE}" ]; then
  echo "Error: Datasets mapping file '${DATASETS_FILE}' does not exist"
  exit 1
fi

while IFS='=' read -r dataset_path passphrase || [ -n "${dataset_path}" ]; do
  # Strip carriage returns in case of Windows line endings
  dataset_path=$(echo "${dataset_path}" | tr -d '\r')
  passphrase=$(echo "${passphrase}" | tr -d '\r')

  # Skip empty lines and comments
  if [ -z "${dataset_path}" ] || echo "${dataset_path}" | grep -q "^#"; then
    continue
  fi

  if [ -z "${passphrase}" ]; then
    echo "Warning: No passphrase found for dataset '${dataset_path}'. Skipping..."
    continue
  fi

  printf "\nDiscovered %s, attempting to unlock now\n" "${dataset_path}"
  # Check if locked
  if is_locked "${dataset_path}"; then
    echo "${dataset_path} is locked, attempting to unlock..."
    unlock "${dataset_path}" "${passphrase}"
  fi
done < "${DATASETS_FILE}"

printf "\nFinished unlock script, exiting...\n"

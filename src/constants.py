"""Shared user-facing reply strings."""

# Sent when the user triggers a reset keyword (a fresh Dify conversation is started).
RESET_REPLY = "已为您开启新对话，请继续提问。"

# Sent when the Dify call fails (timeout, 4xx/5xx, malformed response, ...).
ERROR_REPLY = "服务暂不可用，请稍后重试"

# Sent to Dify as `query` when a file/image arrives with no accompanying text.
FILE_DEFAULT_QUERY = "请处理我发送的文件"

# WeCom media download or AES decryption failed.
FILE_DOWNLOAD_FAILED_REPLY = "文件下载失败，请稍后重试"

# Dify rejected the upload (bad type, server error, app file upload disabled, ...).
FILE_UPLOAD_FAILED_REPLY = "文件上传失败，请稍后重试"

# Pre-checked local size guard or Dify HTTP 413.
FILE_TOO_LARGE_REPLY = "文件超出大小限制（图片最大10MB，其他文件最大15MB），请压缩后重试"

# Downloaded media is empty (0 bytes).
FILE_EMPTY_FILE_REPLY = "文件内容为空，请检查后重新发送"
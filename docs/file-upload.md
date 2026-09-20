## 目标
新需求，增加企业微信机器人传入文件到Dify功能，机器人接收文件如图片或word文档等等，转发给Dify工作流，dify响应消息后返回给企业微信机器人，请先设计方案，与我沟通确认。

## 方案设计

1. 发送文件给企业微信机器人，通过当前程序转发给Dify
2. 切换到feat/upload-file分支开发

## 参考文档
- dify api 消息发送处理文档：https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message
- dify api 文件操作文档：https://docs.dify.ai/zh/api-reference/files/upload-file
- 当前项目使用的机器人长连接文档：https://pypi.org/project/wecom-aibot-python-sdk/

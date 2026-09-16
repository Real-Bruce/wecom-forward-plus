## 目标
开发企业微信转发Dify程序，支持socket方式连接，消息转发路径 Dify <=> wecom-forward-plus <=> 企业微信机器人，请先和我沟通方案设计，确认细节通过后再动手开始写代码

## 架构设计：
1. 使用python开发；
2. 使用企业微信机器人长连接依赖包，参考文档：https://pypi.org/project/wecom-aibot-python-sdk/
3. 通过Dify对外访问的api，调用Dify工作流，参考文档：https://docs.dify.ai/zh/api-reference/chat-messages/send-chat-message


## 功能要求：
1. 将企业微信的账号名，作为Dify消息传入的user信息，并添加wx_前缀，如：企业微信账号名为：zhangsan，则传入Dify的user数据为：wx_zhangsan；
2. 支持多个Dify apikey + 企业微信机器人长连接配置，每个按组区分，即每个企业微信机器人对应一条Dify的api密钥，构成一个配置组；
3. 支持定时开启新的会话，定时重置会话，并设置总会话上限，所有配置组内共享会话总上限。支持单独配置，默认配置为：5分钟重置对话，最大会话上限为2000个；
4. 支持关键字开启新会话，支持单独配置关键词，如：`["开启新对话", "重置对话", "新一轮对话"]` ；
5. 开启新对话关键词不发送Dify，仅在本项目内处理，会话重启后发送："已为您开启新对话，请继续提问。"

## claude要求
1. 项目同步生成readme.md文件，每次功能修改需要同步修改readme文件
2. 项目同步生成Claude.md文件，能通过readme.md文件获取的信息不要写入到claude.md内，claude仅记录项目架构、配置、运行、约束相关信息，项目修改变动后同步修改claude.md文件；
3. `.env、.env.local、secrets/、*.key、*.pem` 为密钥文件：Claude 不得读取、不得 git add（已在 .gitignore）、不得将其真实内容写入任何代码/文档；配置示例仅放 `.env.example` 占位符。
4. 每次修改后提交git，注意使用英文提交；

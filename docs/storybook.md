# 手绘故事动画（video_source = "storybook"）

参考 [hand-drawn-storybook-animation skill](https://github.com/lhlGitHub/hand-drawn-storybook-animation-skill)
的做法：GPT 写剧本 → Claude 用代码画出动画 → 豆包配音。MPT 把整个流程自动化成一个视频来源：

1. **剧本**：当前配置的 LLM 分三轮生成：写故事 → 以挑剔观众和动画导演的角度批判并修改 → 输出分镜 JSON。
   填了「视频文案」时跳过前两轮，直接把文案做成分镜。
2. **配音**：配置了豆包语音（火山引擎语音合成 2.0）就用豆包，旁白和角色使用不同语气；否则用「音频设置」里选的声音。
   逐句合成，按真实时长排时间线，字幕直接取每句的起止时间。
3. **动画**：调用已登录的 `claude` CLI（订阅额度），把 skill 的方法作为系统提示，让 Claude 写出单文件 canvas
   动画 `index.html`，页面暴露确定性的 `STORY.renderFrame(t)`。加载报错或画面全空时，会把问题发回同一会话修复。
4. **录制**：无头 Chromium 逐帧调用 `renderFrame` 截图，ffmpeg 编码。
5. **合成**：沿用普通模式的字幕样式和背景音乐设置，输出 `final-1.mp4`。

中间产物都在 `storage/tasks/<task_id>/storybook/`：`story.json`、`timeline.json`、`index.html`（可以直接用浏览器打开预览）、
`preview-*.jpg`、逐句配音。

## 部署

```bash
# 方式 A：宿主机生成长期令牌
export CLAUDE_CODE_OAUTH_TOKEN="$(claude setup-token)"
export DOUBAO_TTS_API_KEY="..."   # 可选，也可以在 WebUI 设置里填写
docker compose -f docker-compose.storybook.yml up -d --build

# 方式 B：不设令牌启动，然后在容器里登录一次（凭证存在 claude-config 卷）
docker compose -f docker-compose.storybook.yml up -d --build
docker compose -f docker-compose.storybook.yml exec -it webui claude   # 按提示 /login
```

WebUI 里选择「视频来源 → AI 动画 → 手绘故事动画（Claude）」，设置画风、目标时长和画幅即可。

## 配置（config.toml `[app]`）

| 键 | 说明 |
|---|---|
| `storybook_claude_model` | 画动画用的模型，如 `opus`；留空沿用 `claude_code_model_name` 或 CLI 默认 |
| `storybook_claude_effort` | `--effort`，默认 `medium`。过高会长时间思考，可能耗尽单轮输出额度 |
| `storybook_claude_timeout` | 每次调用 claude 的超时秒数，默认 2400 |
| `storybook_fps` | 录制帧率，默认 24 |
| `storybook_fix_attempts` | 页面校验失败时让 Claude 修复的次数，默认 2 |
| `storybook_skill_path` | SKILL.md 路径；镜像内已通过 `STORYBOOK_SKILL_PATH` 指定 |
| `doubao_tts_api_key` / `doubao_tts_voice` / `doubao_tts_resource_id` | 豆包语音；Key 也可用 `DOUBAO_TTS_API_KEY` |

## 耗时参考

2 核 CPU、无 GPU，40 秒 1080p 横屏：剧本约 1.5 分钟，配音十几秒，Claude 画动画约 12 分钟，
录制约 5 分钟，最终合成 2～5 分钟。

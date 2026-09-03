# 181639“图生视频提示词导演”原始 Coze 配置登记

来源：`Workflow-Daily_Update_Te-draft-1256.zip` 内 `Daily_Update_Te-draft.yaml`。

## 节点配置

- 节点类型：LLM，版本 `3`。
- 所属批处理：`生成视频提示词撰写`（195692）。
- 模型：`豆包·2.0·lite`。
- thinking：enabled。
- responseFormat：2（结构化输出）。
- maxTokens：66020。
- 节点超时：180000ms；节点重试次数：3。

## 输入字段和来源

| 字段 | 类型 | 来源/模板 |
|---|---|---|
| `items` | object | 当前批处理项的 `items`，来自 170263 |
| `ref_image` | image[] | 当前批处理项的 `ref_image.ref_image`，标记为视觉输入 |
| `motion_seed` | object | 当前批处理项的 `motion_seed`，来自 170263 |
| `clip_duration` | object | 当前批处理项的 `clip_duration`，来自 170263 |

用户提示词模板原文：

```text
当前镜头组上下文：
{{items}}

当前组动态种子数组：
{{motion_seed.motion_seed}}

当前组分镜头时长数组：
{{clip_duration.clip_duration}}

当前组连续参考图已按时间顺序作为多图输入传入。

请严格依据连续参考图、动态种子和时长数组，输出当前镜头组内全部分镜头的时序规划 JSON。
```

## 系统提示词原文要点

系统提示词标题为“Seedance 1.5 Pro 镜头组时序规划器”，要求模型：

1. 一次处理一个镜头组，依据叙事语义、连续参考图和镜头时长输出结构化动作规划；
2. 锁定首张参考图的媒介、画风、人物/物体、色彩、光源、空间和构图；
3. 每条镜头拆成 2–3 个连续 stages；
4. 非最后镜头趋近下一张参考图，最后镜头低幅度稳定收束；
5. 每条镜头只使用一种镜头状态，运动只允许固定机位、轻微推近、轻微横移、轻微跟随、轻微上升；
6. 禁止字幕、标题、可读文字、数字、logo、水印、聊天气泡、弹窗和 UI 覆盖层；
7. 只输出 JSON，`plans` 数量必须与镜头数量和时长数量一致，按镜头顺序从 0 开始。

示例结构：

```json
{
  "visual_lock": "严格保持首张参考图的视觉媒介、画风、人物外观、服装、材质、色彩、光影和空间构图。",
  "plans": [
    {
      "shot_index": 0,
      "duration": 5,
      "stages": [
        {"time_range": "0.0-2.0s", "action": "可见动作阶段一", "camera_motion": "固定机位"},
        {"time_range": "2.0-5.0s", "action": "承接前一阶段的可见动作", "camera_motion": "固定机位"}
      ]
    }
  ]
}
```

## 输出字段

- `plans`: object[]；每项包含 `shot_index: integer`、`duration: integer`、`stages: object[]`；stage 包含 `action: string`、`camera_motion: string`、`time_range: string`。
- `reasoning_content`: string。
- `visual_lock`: string。

注意：Coze 页面真实输出中还显示了 `reasoning_content`。本地适配器只把它当作声明输出字段保存，不调用模型，也不对其内容做“合理化”改写。

# Dify Prompt 与 JSON Schema（含名字字段）

下面这版可直接放到 Dify 工作流里，目标是让类似下面的原始文本稳定产出可入账消息：

```text
#记账26050104
汤呈工地小卖部
雀巢咖啡2
...
结束
```

## Prompt（建议）

```text
你是记账消息标准化助手。

任务：
1) 从输入文本中提取记账块，保留原有换行，不要改写数字。
2) 输出字段：
   - message: 规范记账块文本（首行 #记账xxxxxxxx，末行 结束）
   - name: 若第2行是名字（非“项目+数量”行），写入该名字；否则输出空字符串 ""。
3) 如果无法识别有效记账块，message 输出空字符串。

格式要求：
- 只输出 JSON，不要解释文字。
- JSON 必须符合给定 schema。
```

## Output Schema（建议）

```json
{
  "type": "object",
  "properties": {
    "messages": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "message": { "type": "string" },
          "name": { "type": "string", "default": "" },
          "timestamp": { "type": "string" },
          "source_id": { "type": "string" }
        },
        "required": ["message", "name"]
      }
    }
  },
  "required": ["messages"]
}
```

## 示例输出

```json
{
  "messages": [
    {
      "message": "#记账26050104\n汤呈工地小卖部\n雀巢咖啡2\n银鹭花生牛奶2\n阿萨姆3\n树叶900茉莉花茶2件\n500茉莉花茶2件\n大泡面：红烧1\n香辣1\n营养快线：香蕉味的1\n1L康师傅：\n冰红茶10\n鲜果橙1\n2L冰红茶3\n费用500红茶1\n结束",
      "name": "汤呈工地小卖部",
      "timestamp": "2026-05-10 00:00:00",
      "source_id": "wechat"
    }
  ]
}
```


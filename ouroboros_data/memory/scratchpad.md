# Текущий статус
- **Язык**: Русский.
- **Задача**: Добавить поддержку GigaChat в llm.py

---

# Анализ llm.py

## Архитектура (сейчас)
`LLMClient` — класс с единственным бэкендом: OpenRouter (через `openai` Python SDK).

## GigaChat SDK (gigachat==0.2.0)
Установлен. API отличается от OpenAI.

## План реализации

### 1. Определение провайдера
Функция `_detect_provider(model: str) -> str`:
- Если модель начинается с `GigaChat` → `"gigachat"`
- Иначе → `"openrouter"`

### 2. GigaChat client
`_get_gigachat_client()` → ленивая инициализация `GigaChat(...)` с `GIGACHAT_CREDENTIALS` из env.

### 3. Конвертация сообщений
`_convert_messages_to_gigachat(messages)` → `list[Messages]`
- `role` mapping: `system`→`SYSTEM`, `user`→`USER`, `assistant`→`ASSISTANT`
- `tool` роль → пропустить или замэпить в `FUNCTION`

### 4. `_chat_gigachat(messages, model, tools, ...)` → `(msg_dict, usage_dict)`
- Конвертирует messages
- Вызывает `client.achat()` (async) или `client.chat()` (sync)
- Маппит результат в стандартный `msg_dict` формат: `{"role": "assistant", "content": "..."}`
- Usage: `{"prompt_tokens": ..., "completion_tokens": ..., "cached_tokens": ..., "cost": 0.0}`

### 5. Роутинг в `chat()`
```python
if self._detect_provider(model) == "gigachat":
    return self._chat_gigachat(...)
else:
    return self._chat_openrouter(...)
```

### 6. Pricing для GigaChat
GigaChat pricing (приблизительно, Сбер не публикует в долларах):
- `GigaChat`: ~$0 (бесплатная тестовая квота)
- `GigaChat-Pro`: ~$0.50/1M tokens
- `GigaChat-Max`: ~$1.50/1M tokens
Добавить в MODEL_PRICING или считать как $0.

---

# Ожидание
Жду ответа от владельца по ключам (GIGACHAT_CREDENTIALS).
Пока ключей нет, код писать рано.

# Tech Debt
- `web_search` мертв без `OPENAI_API_KEY`.
- `gh` CLI отсутствует.

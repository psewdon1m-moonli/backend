# Nano Banana Pro через API — документация и руководство

## Что это

**Nano Banana Pro** — это публичное название модели **`gemini-3-pro-image-preview`** в Gemini API.
Она предназначена для профессиональной генерации и редактирования изображений: сложные композиции, точный текст внутри картинки, product mockups, инфографика, работа с референсами.

---

## Официальные ссылки

### Основная документация
- Image generation (официальный гайд):  
  https://ai.google.dev/gemini-api/docs/image-generation

### Карточка модели Nano Banana Pro
- Gemini 3 Pro Image Preview:  
  https://ai.google.dev/gemini-api/docs/models/gemini-3-pro-image-preview

### Получение API key
- API keys:  
  https://ai.google.dev/gemini-api/docs/api-key

### Общий quickstart Gemini API
- Quickstart:  
  https://ai.google.dev/gemini-api/docs/quickstart

### REST API reference
- Gemini API reference:  
  https://ai.google.dev/api

---

## Каноническое имя модели

Для Nano Banana Pro используй именно:

```text
gemini-3-pro-image-preview
```

---

## Что поддерживает модель

По официальной карточке модели:

- Вход: **Text + Image**
- Выход: **Image + Text**
- Поддерживается **image generation**
- Поддерживается **search grounding**
- Поддерживается **thinking**
- Поддерживается **structured outputs**
- Поддерживается **Batch API**
- **Function calling** не поддерживается
- **Live API** не поддерживается
- **Code execution** не поддерживается
- **File search** не поддерживается
- **Caching** не поддерживается

Ограничения токенов:

- Input token limit: **65,536**
- Output token limit: **32,768**

---

## Быстрый старт

### 1) Получить API ключ

Открой Google AI Studio и создай ключ:

- https://aistudio.google.com/

Официальная инструкция:

- https://ai.google.dev/gemini-api/docs/api-key

### 2) Установить SDK

#### Python
```bash
pip install -U google-genai pillow
```

#### JavaScript / TypeScript
```bash
npm install @google/genai
```

### 3) Задать переменную окружения

#### Linux / macOS
```bash
export GEMINI_API_KEY="YOUR_API_KEY"
```

#### Windows PowerShell
```powershell
$env:GEMINI_API_KEY="YOUR_API_KEY"
```

#### Windows CMD
```cmd
set GEMINI_API_KEY=YOUR_API_KEY
```

---

## Пример: генерация изображения в Python

```python
from google import genai
from google.genai import types
from PIL import Image
import io

client = genai.Client()

prompt = "Create a premium poster for a sci-fi film. Dark background, sharp typography, cinematic lighting."

response = client.models.generate_content(
    model="gemini-3-pro-image-preview",
    contents=[prompt],
    config=types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(
            aspect_ratio="16:9",
            image_size="2K",
        ),
    ),
)

for part in response.candidates[0].content.parts:
    if getattr(part, "inline_data", None):
        image_bytes = part.inline_data.data
        image = Image.open(io.BytesIO(image_bytes))
        image.save("nano_banana_pro_output.png")
        print("Saved: nano_banana_pro_output.png")
```

---

## Пример: генерация изображения через REST

```bash
curl -s -X POST \
  "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-image-preview:generateContent" \
  -H "x-goog-api-key: $GEMINI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [{
      "parts": [
        {"text": "Create a premium cinematic book cover with elegant typography and dramatic rim light"}
      ]
    }],
    "generationConfig": {
      "responseModalities": ["IMAGE"],
      "imageConfig": {
        "aspectRatio": "16:9",
        "imageSize": "2K"
      }
    }
  }'
```

> Ответ возвращает JSON, внутри которого изображение приходит как inline binary payload / encoded image part. На практике удобнее обрабатывать ответ через SDK.

---

## Пример: image-to-image / редактирование по референсу в Python

```python
from google import genai
from google.genai import types
from PIL import Image
import io

client = genai.Client()

reference = Image.open("input.png")
prompt = "Turn this product shot into a clean studio advertisement, keep the object shape intact, improve materials and lighting."

response = client.models.generate_content(
    model="gemini-3-pro-image-preview",
    contents=[prompt, reference],
    config=types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(
            aspect_ratio="1:1",
            image_size="2K",
        ),
    ),
)

for part in response.candidates[0].content.parts:
    if getattr(part, "inline_data", None):
        image = Image.open(io.BytesIO(part.inline_data.data))
        image.save("edited.png")
        print("Saved: edited.png")
```

---

## Работа с несколькими референсами

Официальная документация для Gemini 3 image models указывает, что можно смешивать **до 14 reference images**.
Для **Gemini 3 Pro Image Preview / Nano Banana Pro**:

- до **6** изображений объектов с high-fidelity preservation
- до **5** изображений персонажей для consistency

Это особенно полезно для:

- бренд-гайдов
- product mockups
- поддержания единого персонажа
- сложных коллажей и композиции

---

## Практические настройки

### Когда брать Nano Banana Pro
Используй `gemini-3-pro-image-preview`, когда нужны:

- максимально качественная картинка
- сложный layout
- хороший рендер текста внутри изображения
- product/brand consistency
- инфографика и постеры
- работа с несколькими референсами

### Когда НЕ брать
Если нужен более дешёвый и быстрый потоковый продакшн, обычно лучше смотреть на:

```text
gemini-3.1-flash-image-preview
```

---

## Часто используемые параметры

### `responseModalities`
Обычно для чистой генерации картинки:

```json
["IMAGE"]
```

Если тебе нужен и текст, и изображение:

```json
["TEXT", "IMAGE"]
```

### `imageConfig`
Часто используемые поля:

```json
{
  "aspectRatio": "16:9",
  "imageSize": "2K"
}
```

Типичные aspect ratio:

- `1:1`
- `16:9`
- `9:16`
- `4:3`
- `3:4`
- и другие, поддерживаемые официальной документацией

---

## Рекомендованный минимальный workflow

1. Создать API key в Google AI Studio.
2. Установить `google-genai`.
3. Проверить простой text-to-image запрос.
4. Добавить `response_modalities=["IMAGE"]`.
5. Добавить `image_config` (`aspect_ratio`, `image_size`).
6. Только после этого переходить к image-to-image и multi-reference.
7. Для production — добавить retry/backoff, логирование и валидацию ответа.

---

## Что проверить, если не работает

### 1. Неверное имя модели
Проверь, что используешь:

```text
gemini-3-pro-image-preview
```

### 2. API key не подхватывается
Проверь переменную:

```bash
echo $GEMINI_API_KEY
```

или в PowerShell:

```powershell
echo $env:GEMINI_API_KEY
```

### 3. Неправильный формат `responseModalities`
Для REST обычно:

```json
"responseModalities": ["IMAGE"]
```

### 4. Проблемы с бинарным ответом
Через SDK читать изображение удобнее, чем руками парсить REST-ответ.

### 5. Preview-модель
Модель находится в preview-статусе, поэтому в production стоит учитывать возможные изменения поведения, лимитов и доступности.

---

## Важный нюанс по официальным примерам

На странице `image-generation` есть фрагменты, где комментарий относится к `gemini-3-pro-image-preview`, но в самой строке примера местами остаётся `gemini-3.1-flash-image-preview`.
Если нужна именно Nano Banana Pro, ориентируйся на **карточку модели** и используй:

```text
gemini-3-pro-image-preview
```

---

## Короткая выжимка

- **Nano Banana Pro = `gemini-3-pro-image-preview`**
- Основной официальный гайд: `image-generation`
- Получение ключа: `api-key`
- Для старта лучше Python SDK `google-genai`
- Для text-to-image нужен `responseModalities: ["IMAGE"]`
- Для качества используй `imageConfig.imageSize = "2K"` и нужный `aspectRatio`
- Для сложных задач можно подавать референсы

---

## Источники

- https://ai.google.dev/gemini-api/docs/image-generation
- https://ai.google.dev/gemini-api/docs/models/gemini-3-pro-image-preview
- https://ai.google.dev/gemini-api/docs/api-key
- https://ai.google.dev/gemini-api/docs/quickstart
- https://ai.google.dev/api















в API это обычно сохраняют не как “настройки интерфейса”, а как собственный preset/profile, который вы каждый раз подставляете в запрос. Для Gemini/Nano Banana Pro это раскладывается на три слоя:

параметры вывода — imageConfig.aspectRatio и imageConfig.imageSize;

глобальные правила стиля — systemInstruction;

визуальный якорь — одна и та же reference image, которую вы прикладываете к каждому запросу. Gemini API поддерживает developer-set system instructions, а ImageConfig позволяет задавать aspect ratio и размер изображения. По умолчанию размер — 1K; доступны 1K, 2K, 4K.

Для 2K это делается не “чекбоксом из интерфейса”, а явно в API. Важно: 2K — это не всегда ровно 2048 по длинной стороне, а preset размера, зависящий от aspect ratio. Например, для 1:1 это 2048x2048, а для 16:9 — 2752x1536. Официальные docs ещё и немного несогласованы: в generateContent и image-generation используются значения 1K/2K/4K, а в примерах Interactions API встречается 2k. Для generateContent я бы ориентировался на 2K.

Для ваших длинных правил лучше использовать такую схему:

в systemInstruction положить неизменяемый contract стиля;

в user prompt передавать только переменную часть: кто персонаж, что делает, какая эмоция;

одну и ту же style reference image прикладывать к каждому запросу, если нужна высокая визуальная стабильность.
Gemini 3 image models поддерживают до 14 reference images; для Gemini 3 Pro Image Preview можно использовать до 6 object refs и до 5 character refs.

Практически это выглядит так:

from google import genai
from google.genai import types
from PIL import Image

STYLE_RULES = """
All generated images must follow the same visual style.

REFERENCE RULE
The reference image defines only the visual style:
- stroke thickness
- eye design
- face proportions
- simplicity of shapes

Do NOT copy:
- colors
- character identity
- composition
- exact shapes

Each request must generate a completely new character.

STYLE
flat SVG icon style
simple geometric shapes
clean vector shapes
minimal details
centered composition
white background

VECTOR RULE (strict)
The image must look like a simple SVG icon.
Shapes must be flat and uniform.
No gradients.
No shading.
No lighting.
No color blending.
No soft edges.
No transparency.

Each shape must use a single solid color.

STROKE RULE (strict)
Use a uniform SVG stroke.
Stroke color must be exactly one of the palette colors.
Do NOT create darker or lighter stroke colors.
Stroke must not simulate lighting or shadow.

COLOR PALETTE (strict)
Only the following colors are allowed:
blue   #4A9AD4
red    #FF1F2D
pink   #EC6A9E
yellow #F5E617
white  #FFFFFF
black  #000000

No other colors are allowed.

FILL RULE
All fills must be solid flat colors.
Each shape must use exactly one color from the palette.

FACE STRUCTURE (universal)
All characters share the same face structure.
large rounded head
two simple eyes
small nose
simple smiling mouth
optional round cheeks

CHARACTER TYPE RULE
Animals:
simple animal mouth and nose

Humans:
small round nose (dot or oval)
simple curved smile
no animal muzzle

DESIGN RULES
large rounded head
simple shapes
vector friendly
minimal details

GENERATION RULE
Every request must generate a completely new illustration.
Never modify a previous image.
Never reproduce the reference character.
Always keep the same visual style across all characters.
The result should look like a simple exportable SVG icon.
"""

PRESET = {
    "model": "gemini-3-pro-image-preview",
    "aspect_ratio": "1:1",
    "image_size": "2K",
    "style_ref": "style_reference.png",
}

client = genai.Client()

response = client.models.generate_content(
    model=PRESET["model"],
    contents=[
        "Create a completely new human character: a cheerful young librarian, front view, waving.",
        Image.open(PRESET["style_ref"]),
    ],
    config=types.GenerateContentConfig(
        system_instruction=STYLE_RULES,
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(
            aspect_ratio=PRESET["aspect_ratio"],
            image_size=PRESET["image_size"],
        ),
    ),
)

for part in response.parts:
    if image := part.as_image():
        image.save("result.png")

Здесь ключевая идея такая:
preset хранит модель, размер, ratio и путь к эталонной картинке;
system instruction хранит строгие правила;
user prompt хранит только содержание текущей иллюстрации.

Для продакшна я бы сохранял это в JSON/YAML у себя на бэкенде, например так:

{
  "name": "flat_svg_icon_style_v1",
  "model": "gemini-3-pro-image-preview",
  "generation_config": {
    "aspect_ratio": "1:1",
    "image_size": "2K"
  },
  "system_instruction": "LONG STYLE CONTRACT...",
  "reference_images": [
    "style_reference.png"
  ]
}

Дальше сервер делает простую сборку:

загружает preset,

подставляет текущий user prompt,

прикладывает ref image,

отправляет единый запрос в Gemini.

Если такие длинные инструкции будут использоваться много раз подряд, их можно временно держать в Context Caching. У кэша TTL по умолчанию 1 час, его можно обновлять; это полезно для снижения стоимости и чтобы не пересылать один и тот же большой instruction block каждый раз. Но это именно временное хранение — для постоянного preset всё равно лучше хранить конфиг у себя в приложении.

Ещё один важный нюанс: даже если вы пишете “simple exportable SVG icon”, Gemini image generation возвращает image output и в официальных примерах результат сохраняется как PNG / binary image data. То есть модель может сделать картинку в стиле SVG, но это не настоящий .svg. Если вам нужен реальный векторный файл, после генерации нужен отдельный этап векторизации или собственный SVG-рендерер поверх структурированного описания.

Самая надёжная схема для вашего кейса:

не полагаться на память сессии;

каждый запрос собирать заново из preset;

всегда прикладывать одну и ту же style reference;

не отправлять предыдущие сгенерированные картинки обратно, если правило действительно “never modify a previous image”;

хранить preset как версионируемый артефакт: style_v1, style_v2, style_v3.
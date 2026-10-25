# Архитектура и модель данных

Модульный монолит: React/TypeScript/Vite → FastAPI/Pydantic → SQLAlchemy → PostgreSQL. Alembic управляет схемой. Локальный режим без Docker использует SQLite с теми же моделями. Внешние проверки являются функциями сервера и не требуют сетевого доступа.

```mermaid
flowchart LR
    E[Предприниматель] --> UI[React: кабинет клиента]
    B[Сотрудник банка] --> UI2[React: очередь и карточка банка]
    UI --> WEB[FastAPI: статическая production-сборка]
    UI2 --> WEB
    WEB --> API[FastAPI /api]
    API --> AUTH[Сессии, CSRF, роли, компания]
    API --> FLOW[Действия, версии, идемпотентность]
    FLOW --> CHECK[Локальные проверки реестра и БКИ]
    CHECK --> SCORE[Объяснимый риск demo-v1]
    SCORE --> POLICY[Продуктовая политика]
    FLOW --> DB[(PostgreSQL)]
    POLICY --> DB
    AUTH --> DB
    DB --- DATA[Заявки, документы, аудит, уведомления]
```

## Границы модулей

| Область | Ответственность |
|---|---|
| `frontend/src` | Формы, калькулятор, серверные карточки и списки, роль клиента/банка, уведомления |
| `backend/app/main.py` | HTTP API, маршруты, права на ресурс, транзакционные действия и представления по ролям |
| `backend/app/schemas.py` | Валидация входных полей и финансовых ограничений |
| `backend/app/security.py` | Хеши паролей, сессия, CSRF, роли и граница транзакции |
| `backend/app/lending.py` | Каталог, Decimal-расчет, переходы, локальные проверки, риск и продуктовая политика |
| `backend/app/models.py` | Реляционная модель и сохраненные JSON-снимки |
| `backend/app/db.py` | Соединение, SQLAlchemy session, SQLite-параметры |
| `backend/alembic` | Версионирование схемы БД |

## Транзакции, повторы и конкурентность

Создание и действия используют ключи `Idempotency-Key`, привязанные к пользователю и области операции. Сохраняются хеш запроса и ответ. Повтор с тем же ключом/телом не должен повторять действие; изменение тела с прежним ключом вызывает конфликт. Для нового намерения нужен новый ключ.

Изменения заявки требуют её актуальную `version`. На PostgreSQL значимые действия блокируют строку, на SQLite записи сериализуются через `BEGIN IMMEDIATE`. Состояние, история, аудит, уведомления и запись идемпотентности фиксируются в одной транзакции. Версия защищает от одновременных несовместимых действий. База и документы переживают обычный перезапуск при сохранении хранилища.

Проверки выполняются синхронно. `submitted`, `checking`, `scoring` фиксируются в истории в той же операции; интерфейс получает конечный результат сервера. Отдельных задач и очереди нет, поэтому рестарт не требует восстановления воркеров. Предсказуемый сбой симулятора сохраняется как `check_failed` с явным повтором.

## Состояния

```mermaid
stateDiagram-v2
    [*] --> draft
    draft --> submitted: submit
    submitted --> checking
    checking --> check_failed: источник недоступен
    check_failed --> checking: retry
    checking --> needs_info: неполные данные
    needs_info --> checking: respond
    checking --> rejected: неактивная компания
    checking --> scoring: данные полны
    scoring --> approved: автоматическое одобрение
    scoring --> rejected: отказ
    scoring --> alternative_offered: меньшая сумма
    scoring --> manual_review: нужна экспертиза
    manual_review --> approved: approve + причина
    manual_review --> rejected: reject + причина
    manual_review --> alternative_offered: offer + причина
    manual_review --> needs_info: request_info
    approved --> accepted: accept
    alternative_offered --> accepted: accept
    approved --> declined: decline
    alternative_offered --> declined: decline
    accepted --> signed: sign
    signed --> disbursed: disburse
    draft --> cancelled: cancel
    needs_info --> cancelled: cancel
    check_failed --> cancelled: cancel
    manual_review --> cancelled: cancel
    approved --> cancelled: cancel
    alternative_offered --> cancelled: cancel
    disbursed --> [*]
    declined --> [*]
    rejected --> [*]
    cancelled --> [*]
```

Автор и роль проверяются отдельно от самого графа. Банк не получает права клиента только потому, что видит его заявку. Терминальные состояния не разрешают переиграть выдачу или вернуть заявку в черновик. Полный актуальный набор разрешенных действий задаёт сервер; диаграмма объясняет бизнес-процесс.

## ER-модель

```mermaid
erDiagram
    COMPANIES ||--o{ USERS : employs
    COMPANIES ||--o{ APPLICATIONS : owns
    USERS ||--o{ SESSIONS : authenticates
    USERS ||--o{ NOTIFICATIONS : receives
    USERS ||--o{ IDEMPOTENCY : submits
    APPLICATIONS ||--o{ DOCUMENTS : stores
    APPLICATIONS ||--o{ NOTES : contains
    APPLICATIONS ||--o{ AUDIT : tracks
    APPLICATIONS ||--o{ NOTIFICATIONS : triggers
    COMPANIES {
        uuid id PK
        string inn UK
        string name
        integer registration_months
        decimal revenue
        decimal expenses
        decimal monthly_debt
        json fixture
    }
    USERS {
        uuid id PK
        string email UK
        uuid company_id FK
        string role
        string password_hash
    }
    SESSIONS {
        string id PK
        uuid user_id FK
        string csrf_token
        datetime expires_at
    }
    APPLICATIONS {
        uuid id PK
        string number UK
        uuid company_id FK
        string product_id
        decimal amount
        integer months
        string status
        integer version
        json company_snapshot
        json calculation
        json risk
        json checks
        json recommendation
        json decision
        json offer
        json evaluation_history
        json history
        json responses
        json disbursement
    }
    DOCUMENTS {
        uuid id PK
        uuid application_id FK
        string name
        string media_type
        integer size
        binary content
    }
    NOTES {
        uuid id PK
        uuid application_id FK
        string actor
        string message
    }
    AUDIT {
        uuid id PK
        uuid application_id FK
        string actor
        string action
        string reason
    }
    NOTIFICATIONS {
        uuid id PK
        uuid user_id FK
        uuid application_id FK
        string message
        boolean read
    }
    IDEMPOTENCY {
        uuid id PK
        uuid user_id FK
        string scope
        string key
        string request_hash
        json response
    }
    LOGIN_ATTEMPTS {
        string id PK
        integer count
        datetime window_start
    }
```

Поля времени и финансовые поля заявки сокращены на диаграмме для читаемости. Реальная схема — `backend/app/models.py` и миграции. `LOGIN_ATTEMPTS` хранит счетчик окна входа. `fixture` — серверные данные симулятора, не часть редактируемого профиля. `product_id` ссылается на каталог в коде; отдельной таблицы продуктов нет. Рекомендации, решения, предложения, истории и выдача вложены в JSON заявки, а не представлены независимыми сущностями.

Документы хранятся бинарно в БД. Это упрощает атомарность и резервное копирование демо. При промышленном объёме нужен объектный storage с отдельными политиками доступа, сканированием и жизненным циклом; сейчас такого сервиса нет.

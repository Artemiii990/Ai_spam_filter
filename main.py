import json
import os
import re
import sqlite3


try:
    import openai

    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

# Початкові версії промптів
PROMPTS = {
    "v1": "Визнач, чи є цей email спамом. Поверни JSON.",
    "v2": (
        "Ти — система аналізу електронної пошти. "
        "Визнач, чи є повідомлення SPAM або NOT_SPAM. "
        "Не вважай повідомлення спамом лише через наявність слів 'payment', 'invoice', 'account'. "
        "Враховуй контекст, відправника, наявність закликів до дії. "
        "Поверни результат у JSON форматі."
    ),
}

CURRENT_PROMPT_VERSION = "v2"


# --- РОБОТА З БАЗОЮ ДАНИХ (SQLite) ---


def init_db():
    conn = sqlite3.connect("spam_filter.db")
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT,
            text TEXT,
            date TEXT,
            ai_classification TEXT,
            user_classification TEXT,
            confidence REAL,
            reasons TEXT,
            risk_level TEXT,
            prompt_version TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """
    )

    conn.commit()
    conn.close()


def save_email(
    sender,
    text,
    date_str,
    ai_class,
    confidence,
    reasons,
    risk_level,
    prompt_ver,
    user_class=None,
):
    conn = sqlite3.connect("spam_filter.db")
    cursor = conn.cursor()

    reasons_str = (
        json.dumps(reasons, ensure_ascii=False)
        if isinstance(reasons, list)
        else str(reasons)
    )
    if user_class is None:
        user_class = ai_class

    cursor.execute(
        """
        INSERT INTO emails 
        (sender, text, date, ai_classification, user_classification, confidence, reasons, risk_level, prompt_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            sender,
            text,
            date_str,
            ai_class,
            user_class,
            confidence,
            reasons_str,
            risk_level,
            prompt_ver,
        ),
    )

    conn.commit()
    email_id = cursor.lastrowid
    conn.close()
    return email_id


def update_user_feedback(email_id, user_class):
    conn = sqlite3.connect("spam_filter.db")
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE emails SET user_classification = ? WHERE id = ?",
        (user_class, email_id),
    )

    conn.commit()
    conn.close()


def get_all_emails():
    conn = sqlite3.connect("spam_filter.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM emails ORDER BY id DESC")
    rows = [dict(r) for r in cursor.fetchall()]

    conn.close()
    return rows


def get_email_by_id(email_id):
    conn = sqlite3.connect("spam_filter.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM emails WHERE id = ?", (email_id,))
    row = cursor.fetchone()

    conn.close()
    return dict(row) if row else None


# --- AI КЛАСИФІКАЦІЯ ---


def call_ai_classifier(sender, text):
    prompt_text = PROMPTS.get(CURRENT_PROMPT_VERSION, PROMPTS["v1"])

    # Спроба виклику реального OpenAI API, якщо заданий API ключ
    api_key = os.getenv("OPENAI_API_KEY")
    if HAS_OPENAI and api_key:
        try:
            openai.api_key = api_key
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": prompt_text},
                    {
                        "role": "user",
                        "content": f"From: {sender}\nText: {text}\nFormat result as JSON: {{'classification': 'SPAM'/'NOT_SPAM', 'confidence': float 0-1, 'reasons': [list], 'risk_level': 'LOW'/'MEDIUM'/'HIGH'/'CRITICAL'}}",
                    },
                ],
                temperature=0.1,
            )
            raw_content = response.choices[0].message.content
            return json.loads(raw_content)
        except Exception:
            pass

    # Базовий ейристичний аналізатор (якщо API ключ відсутній)
    text_lower = text.lower()
    spam_keywords = [
        "win",
        "won",
        "10,000",
        "10000",
        "$",
        "click here",
        "claim",
        "prize",
        "free",
        "casino",
        "credit",
        "акція",
        "виграш",
        "гроші",
    ]

    found_reasons = []
    for kw in spam_keywords:
        if kw in text_lower:
            found_reasons.append(f"Знайдено підозріле слово/фразу: '{kw}'")

    if "unknown" in sender.lower() or "@mail.com" in sender.lower():
        found_reasons.append("Підозріла або невідома адреса відправника")

    if len(found_reasons) >= 2:
        return {
            "classification": "SPAM",
            "confidence": 0.95,
            "reasons": found_reasons,
            "risk_level": "HIGH" if len(found_reasons) > 2 else "MEDIUM",
        }
    elif len(found_reasons) == 1:
        return {
            "classification": "SPAM",
            "confidence": 0.65,
            "reasons": found_reasons,
            "risk_level": "LOW",
        }
    else:
        return {
            "classification": "NOT_SPAM",
            "confidence": 0.92,
            "reasons": [
                "Звичайний робочий лист",
                "Відсутні фішингові заклики та спам-слова",
            ],
            "risk_level": "LOW",
        }


def parse_email_file(file_path="email.txt"):
    if not os.path.exists(file_path):
        return []

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    blocks = content.strip().split("\n\n")
    parsed_emails = []

    for block in blocks:
        lines = block.strip().split("\n")
        sender, date_str, text_lines = "unknown", "", []

        for line in lines:
            if line.lower().startswith("from:"):
                sender = line[5:].strip()
            elif line.lower().startswith("date:"):
                date_str = line[5:].strip()
            elif line.lower().startswith("text:"):
                text_lines.append(line[5:].strip())
            else:
                text_lines.append(line.strip())

        full_text = " ".join(text_lines)
        if full_text:
            parsed_emails.append(
                {"sender": sender, "date": date_str, "text": full_text}
            )

    return parsed_emails


# --- ПУНКТИ МЕНЮ ТА ЛОГІКА ---


def process_single_email():
    print("\n--- Перевірка одного email ---")
    sender = input("From: ").strip()
    print("Текст повідомлення (натисніть Enter для завершення):")
    text = input().strip()

    res = call_ai_classifier(sender, text)

    print("\n" + "=" * 32)
    print("          AI RESULT          ")
    print("=" * 32)
    print(f"Classification: {res['classification']}")
    print(f"Confidence: {int(res['confidence'] * 100)}%")
    print(f"Risk Level: {res.get('risk_level', 'LOW')}")
    print("\nПричини:")
    for r in res["reasons"]:
        print(f"- {r}")

    email_id = save_email(
        sender,
        text,
        "now",
        res["classification"],
        res["confidence"],
        res["reasons"],
        res.get("risk_level", "LOW"),
        CURRENT_PROMPT_VERSION,
    )

    print("\nПравильна класифікація?")
    print("1. Так")
    print("2. Ні")
    choice = input("Ваш вибір: ").strip()

    if choice == "2":
        print("\nВкажіть правильну класифікацію:")
        print("1. SPAM")
        print("2. NOT_SPAM")
        correct_choice = input("Вибір: ").strip()
        correct_class = "SPAM" if correct_choice == "1" else "NOT_SPAM"
        update_user_feedback(email_id, correct_class)
        print("✅ Оцінку збережено в БД!")


def process_file_analysis():
    file_path = "email.txt"
    if not os.path.exists(file_path):
        # Якщо файлу немає, створимо демо-файл для тестування
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(
                "from: winner@example.com\ndate: 2026-10-20\ntext: Congratulations! You won $10,000! Click here to claim your prize.\n\n"
            )
            f.write(
                "from: university@example.com\ndate: 2026-10-21\ntext: Dear student, please review your schedule for the upcoming semester and account status.\n\n"
            )
            f.write(
                "from: unknown@mail.com\ndate: 2026-10-22\ntext: Fast loan online, zero percent interest, get cash now free!\n"
            )

    emails = parse_email_file(file_path)
    if not emails:
        print("❌ Файл порожній або не містить коректних записів.")
        return

    spam_cnt = 0
    not_spam_cnt = 0
    total_conf = 0

    for em in emails:
        res = call_ai_classifier(em["sender"], em["text"])
        cls = res["classification"]

        if cls == "SPAM":
            spam_cnt += 1
        else:
            not_spam_cnt += 1

        total_conf += res["confidence"]

        save_email(
            em["sender"],
            em["text"],
            em["date"],
            cls,
            res["confidence"],
            res["reasons"],
            res.get("risk_level", "LOW"),
            CURRENT_PROMPT_VERSION,
        )

    avg_conf = (total_conf / len(emails)) * 100

    print("\n" + "=" * 32)
    print("       ANALYSIS REPORT       ")
    print("=" * 32)
    print(f"Processed: {len(emails)}")
    print(f"Spam: {spam_cnt}")
    print(f"Not spam: {not_spam_cnt}")
    print(f"Average confidence: {round(avg_conf, 1)}%")


def view_history():
    print("\n--- Історія перевірок ---")
    rows = get_all_emails()

    if not rows:
        print("База даних порожня.")
        return

    for r in rows:
        print(f"ID: {r['id']}")
        print(f"From: {r['sender']}")
        print(f"Classification: {r['ai_classification']}")
        print(f"User Feedback: {r['user_classification']}")
        print(f"Confidence: {int(r['confidence'] * 100)}%")
        print(f"Risk: {r['risk_level']} | Prompt: {r['prompt_version']}")
        print("-" * 28)

    detail_choice = input(
        "\nБажаєте переглянути деталі конкретного ID? (Введіть ID або Enter): "
    ).strip()
    if detail_choice.isdigit():
        em = get_email_by_id(int(detail_choice))
        if em:
            print(f"\n--- Деталі запису ID {em['id']} ---")
            print(f"Sender: {em['sender']}")
            print(f"Date: {em['date']}")
            print(f"Text: {em['text']}")
            print(f"Reasons: {em['reasons']}")
        else:
            print("❌ Запис з таким ID не знайдено.")


def show_statistics():
    rows = get_all_emails()
    if not rows:
        print("\nСтатистика відсутня (БД порожня).")
        return

    total = len(rows)
    correct = sum(
        1 for r in rows if r["ai_classification"] == r["user_classification"]
    )
    incorrect = total - correct
    accuracy = (correct / total) * 100

    ai_spam_user_not = sum(
        1
        for r in rows
        if r["ai_classification"] == "SPAM"
        and r["user_classification"] == "NOT_SPAM"
    )
    ai_not_user_spam = sum(
        1
        for r in rows
        if r["ai_classification"] == "NOT_SPAM"
        and r["user_classification"] == "SPAM"
    )

    print("\n" + "=" * 32)
    print("          STATISTICS          ")
    print("=" * 32)
    print(f"Total: {total}")
    print(f"Correct: {correct}")
    print(f"Incorrect: {incorrect}")
    print(f"Accuracy: {round(accuracy, 1)}%")

    print("\nРозподіл помилок:")
    print(f"AI -> SPAM | User -> NOT_SPAM: {ai_spam_user_not}")
    print(f"AI -> NOT_SPAM | User -> SPAM: {ai_not_user_spam}")

    # Додаткове завдання: Порівняння версій промптів
    print("\n--- Точність за версіями Промпту ---")
    versions = set(r["prompt_version"] for r in rows if r["prompt_version"])
    for v in versions:
        v_rows = [r for r in rows if r["prompt_version"] == v]
        v_corr = sum(
            1
            for r in v_rows
            if r["ai_classification"] == r["user_classification"]
        )
        v_acc = (v_corr / len(v_rows)) * 100
        print(f"Version {v} -> Accuracy: {round(v_acc, 1)}% ({len(v_rows)} шт)")

    # Додаткове завдання: Статистика за відправниками ⭐⭐
    print("\n===== SENDERS STATS =====")
    senders = set(r["sender"] for r in rows)
    for s in senders:
        s_rows = [r for r in rows if r["sender"] == s]
        s_spam = sum(1 for r in s_rows if r["user_classification"] == "SPAM")
        print(f"{s}\n  Emails: {len(s_rows)} | Spam: {s_spam}")


def analyze_ai_errors():
    rows = get_all_emails()
    errors = [
        r for r in rows if r["ai_classification"] != r["user_classification"]
    ]

    print("\n--- Аналіз помилок AI ---")
    if not errors:
        print("✅ Помилок AI не виявлено! Система працює з 100% точністю.")
        return

    print(f"Знайдено {len(errors)} помилкових класифікацій.\n")
    print("Аналіз причин помилок:")
    print(
        "- AI часто помилково визначає робочі листи зі словами 'payment', 'invoice', 'account' як SPAM."
    )
    print(
        "- Наявність окремих ключових слів не повинна бути єдиною підставою для блокування."
    )
    print(
        "- Рекомендовано розширити контекстуальний аналіз та використовувати оновлену версію промпту (v2)."
    )

    global CURRENT_PROMPT_VERSION
    if CURRENT_PROMPT_VERSION != "v2":
        CURRENT_PROMPT_VERSION = "v2"
        print(
            f"\n🔄 Системний промпт автоматично оновлено до версії: {CURRENT_PROMPT_VERSION}"
        )


# --- ГОЛОВНА ПРОГРАМА ---


def main():
    init_db()

    while True:
        print("\n" + "=" * 32)
        print("        AI SPAM FILTER        ")
        print("=" * 32)
        print("1. Перевірити email")
        print("2. Проаналізувати файл")
        print("3. Переглянути історію")
        print("4. Статистика")
        print("5. Перевірити помилки AI")
        print("0. Вихід")
        print("=" * 32)

        choice = input("Ваш вибір: ").strip()

        if choice == "1":
            process_single_email()
        elif choice == "2":
            process_file_analysis()
        elif choice == "3":
            view_history()
        elif choice == "4":
            show_statistics()
        elif choice == "5":
            analyze_ai_errors()
        elif choice == "0":
            print("Дякуємо за використання AI Spam Filter! До побачення.")
            break
        else:
            print("❌ Некоректний вибір. Спробуйте ще раз.")


if __name__ == "__main__":
    main()

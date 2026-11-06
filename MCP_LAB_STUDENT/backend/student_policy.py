"""Учебные правила: меняйте только allow / confirm / deny, затем запускайте проверки."""
from urllib.parse import urlparse
# DRIVE_FOLDER = ""   # URL вашей учебной папки — только для drive-сценария

POLICIES = {
#    # === ВАША ПОЛИТИКА (этап 2 лабы) ===============================
#    # Ключ — имя вашего сценария из SCENARIOS (scenarios.py).
#    # Значение — решение по каждому инструменту:
#    #   allow   — разрешить,
#    #   confirm — спросить человека перед вызовом,
#    #   deny    — запретить (это и значение по умолчанию для
#    #           неуказанных инструментов).
#    #
#    # "my_site": {
#    #     "browser_navigate": "allow",
#    #     "browser_snapshot": "allow",
#    #     "browser_scroll":   "allow",
#    #     "browser_get_text": "allow",
#    #     "browser_back":     "allow",
#    #     "browser_click":    "confirm",
#    #     "browser_type":     "deny",
#    #     "browser_press":    "deny",
#    #     "browser_new_tab":  "deny",
#    #     "browser_switch_tab": "deny",
#    #     "browser_list_tabs": "deny",
#    #     "drive_list_files": "deny",
#    #     "drive_trash_files": "deny",
#    # },
}


# Запрет конкретных элементов ПО ИМЕНИ — гранулярнее инструмента:
# browser_click в целом может быть confirm/allow, но эти кнопки —
# под запретом всегда. Имена матчатся regex'ом против имени
# элемента из снапшота (то, что видит модель в списке элементов).
DENY_NAME_PATTERNS = {
#    # === ВАШИ ЗАПРЕЩЁННЫЕ ЭЛЕМЕНТЫ =================================
#    # Ключ — имя сценария; значение — regex'ы на имя элемента.
#    # Клик по элементу с таким именем — deny даже если
#    # browser_click = allow/confirm.
#    # "my_site": [r"оплатить", r"удалить", r"\bpay\b", r"delete"],
}


def validate_policy(rules, known_tools=None):
    """Ошибки политики явные; неизвестные инструменты не разрешаются автоматически."""
    if not isinstance(rules, dict):
        raise ValueError("policy must be a dictionary")
    for name, verdict in rules.items():
        if verdict not in ("allow", "confirm", "deny"):
            raise ValueError(f"invalid policy verdict for {name}: {verdict!r}")
        if known_tools is not None and name not in known_tools:
            raise ValueError(f"unknown tool in policy: {name}")


def drive_workspace_ok(url):
    """Доступ разрешён к одной явно настроенной учебной папке, не ко всему аккаунту."""
    value, allowed = urlparse(url), urlparse(DRIVE_FOLDER)
    return value.scheme == allowed.scheme and value.netloc == allowed.netloc and value.path.rstrip("/") == allowed.path.rstrip("/")

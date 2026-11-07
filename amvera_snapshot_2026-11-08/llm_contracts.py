"""Local validation, never trust model output as executable instructions."""
import json

def validate_connection(raw):
    if not isinstance(raw, str) or len(raw) > 12000:
        raise ValueError('Ответ пустой или слишком длинный')
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {'schema_version', 'status', 'answer'}:
        raise ValueError('Неверная схема ответа')
    if value['schema_version'] != '1' or value['status'] != 'ok' or value['answer'] != 'Жорик на связи':
        raise ValueError('Контрольные значения не совпали')
    return value

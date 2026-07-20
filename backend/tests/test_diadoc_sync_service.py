import base64

from app.services.diadoc_sync_service import _entity_content, _is_document_entity, _last_index_key


def test_diadoc_entity_detection_and_content():
    entity = {
        "EntityType": "Attachment",
        "TypeNamedId": "UniversalTransferDocument",
        "EntityId": "entity-1",
        "Content": {"Data": base64.b64encode(b"<xml/>").decode("ascii")},
    }
    assert _is_document_entity(entity) is True
    assert _entity_content(entity) == b"<xml/>"


def test_last_index_key_uses_last_event():
    assert _last_index_key([{"IndexKey": "one"}, {"IndexKey": "two"}]) == "two"

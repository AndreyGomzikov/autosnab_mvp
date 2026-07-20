from app.services.diadoc_xml_parser_service import parse_diadoc_invoice_xml


def test_parse_diadoc_utd_xml():
    xml = '''<?xml version="1.0" encoding="utf-8"?>
    <Файл>
      <Документ>
        <СвСчФакт НомерСчФ="A-15" ДатаСчФ="16.07.2026"/>
        <СвПрод><ИдСв><СвЮЛУч НаимОрг="ООО Поставщик" ИННЮЛ="7701000000"/></ИдСв></СвПрод>
        <ТаблСчФакт СтТовУчНалВсего="1200.00">
          <СведТов НаимТов="Вода 1,5 л" КолТов="2" ЦенаТов="500" СтТовУчНал="1000" НалСт="20%" ОКЕИ_Тов="уп" КодТов="W-1"/>
        </ТаблСчФакт>
      </Документ>
    </Файл>'''.encode('utf-8')
    payload = parse_diadoc_invoice_xml(xml, file_id="m:e")
    assert payload.invoice_number == "A-15"
    assert payload.supplier == "ООО Поставщик"
    assert payload.supplier_inn == "7701000000"
    assert payload.total_sum == 1200.0
    assert len(payload.items) == 1
    assert payload.items[0].name == "Вода 1,5 л"
    assert payload.items[0].quantity == 2.0
    assert payload.items[0].price == 500.0
    assert payload.items[0].product_code == "W-1"

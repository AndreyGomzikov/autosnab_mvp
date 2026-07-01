from __future__ import annotations


PURCHASER_TABLE_MENU_SCRIPT = """function onOpen() {
  addPurchaserTableMenu_();
}

function installPurchaserTableMenu() {
  addPurchaserTableMenu_();
}

function addPurchaserTableMenu_() {
  const ui = getPurchaserUi_();

  if (!ui) {
    Logger.log(
      'Меню можно добавить только из открытой Google Таблицы. Открой таблицу и обнови страницу.'
    );
    return;
  }

  ui.createMenu('Меню кнопок')
    .addItem('Обновить товары в таблицах заведений', 'syncVenueProductsFromDirectory')
    .addSeparator()
    .addItem('Создать коды и инвайт-ссылки', 'createSupplierInviteLinks')
    .addSeparator()
    .addItem('Отправить заявки поставщику', 'sendCheckedSupplierRequests')
    .addSeparator()
    .addItem('Обновить измененные заявки', 'updateChangedRequestsFromBase')
    .addSeparator()
    .addItem('Chat ID', 'maxDebugListChats')
    .addToUi();
}

function getPurchaserUi_() {
  try {
    return SpreadsheetApp.getUi();
  } catch (error) {
    return null;
  }
}
"""


def build_purchaser_table_menu_script() -> str:
    return PURCHASER_TABLE_MENU_SCRIPT

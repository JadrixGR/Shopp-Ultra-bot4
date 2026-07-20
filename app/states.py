from aiogram.fsm.state import State, StatesGroup


class DepositStates(StatesGroup):
    waiting_amount = State()
    waiting_transaction_id = State()


class AddProductStates(StatesGroup):
    waiting_name = State()
    waiting_price = State()
    waiting_emoji = State()
    waiting_description = State()
    waiting_instructions = State()
    waiting_media = State()
    waiting_stock = State()
    waiting_confirmation = State()


class EditProductStates(StatesGroup):
    waiting_value = State()


class AddStockStates(StatesGroup):
    waiting_items = State()


class EditSettingStates(StatesGroup):
    waiting_value = State()


class AnnouncementStates(StatesGroup):
    waiting_content = State()
    waiting_confirmation = State()


class RefundStates(StatesGroup):
    waiting_search = State()
    waiting_total_days = State()
    waiting_used_days = State()
    waiting_confirmation = State()


class BalanceAdjustmentStates(StatesGroup):
    waiting_user_id = State()
    waiting_amount = State()
    waiting_reason = State()
    waiting_confirmation = State()


class PurchaseQuantityStates(StatesGroup):
    waiting_custom_quantity = State()


class ExternalPurchaseStates(StatesGroup):
    waiting_customer_email = State()
    waiting_slot_months = State()
    waiting_confirmation = State()


class AppearanceStates(StatesGroup):
    waiting_button_label = State()
    waiting_button_emoji = State()
    waiting_text_template = State()
    waiting_test_emoji = State()

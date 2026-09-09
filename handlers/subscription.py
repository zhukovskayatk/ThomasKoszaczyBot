"""
Экран "💎 Premium" и оплата звёздами Telegram (Telegram Stars).

Как это устроено физически (см. документацию Telegram Bot API про
Stars-платежи):
1. Бот показывает счёт (message.answer_invoice) с currency="XTR" и
   provider_token="" — для звёзд отдельный платёжный провайдер не нужен,
   Telegram обрабатывает списание сам, банковские данные бот не видит
   вообще никогда.
2. Человек нажимает "Оплатить" в самом Telegram — тот присылает боту
   pre_checkout_query, на который ОБЯЗАТЕЛЬНО нужно ответить в течение
   10 секунд (см. precheckout ниже), иначе Telegram сам отменит платёж.
3. После успешного списания Telegram присылает обычное сообщение с полем
   successful_payment — вот тут и продлеваем подписку в БД (см.
   database.requests.extend_premium).

Подписка = единственное поле User.premium_until в БД (см.
database/models.py) — партнёрский режим и любые другие будущие
Premium-фичи должны проверять её через database.requests.is_premium_active,
а не заводить собственный флаг "платный ли пользователь".
"""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

import texts
from database.requests import extend_premium, get_or_create_user, is_premium_active
from keyboards import PREMIUM_BUTTON_TEXT, premium_buy_keyboard

router = Router(name="subscription")

# Цена и срок подписки — единственное место, где их нужно менять, если
# решим пересчитать тариф.
PREMIUM_PRICE_STARS = 199
PREMIUM_DURATION_DAYS = 30

# Свой идентификатор счёта — sверяем его же в pre_checkout_query, просто
# чтобы не подтверждать вслепую платёж с чужим/неожиданным payload.
_INVOICE_PAYLOAD = "premium_30d"


async def show_premium_screen(message: Message) -> None:
    user = await get_or_create_user(user_id=message.from_user.id, username=message.from_user.username)
    active = is_premium_active(user)
    await message.answer(
        texts.premium_status_text(active, user.premium_until, PREMIUM_PRICE_STARS, PREMIUM_DURATION_DAYS),
        reply_markup=premium_buy_keyboard(active).as_markup(),
    )


@router.message(F.text == PREMIUM_BUTTON_TEXT)
async def premium_button(message: Message) -> None:
    await show_premium_screen(message)


@router.message(Command("premium"))
async def cmd_premium(message: Message) -> None:
    await show_premium_screen(message)


@router.callback_query(F.data == "premium_buy")
async def premium_buy(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer_invoice(
        title="Thomas Koszaczy Premium",
        description=f"Партнёрский режим и будущие Premium-фичи на {PREMIUM_DURATION_DAYS} дней.",
        payload=_INVOICE_PAYLOAD,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"Premium на {PREMIUM_DURATION_DAYS} дней", amount=PREMIUM_PRICE_STARS)],
    )


@router.pre_checkout_query()
async def precheckout(pre_checkout_query: PreCheckoutQuery) -> None:
    if pre_checkout_query.invoice_payload != _INVOICE_PAYLOAD:
        await pre_checkout_query.answer(
            ok=False, error_message="Что-то пошло не так со счётом — попробуй ещё раз через /premium."
        )
        return
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    new_until = await extend_premium(user_id=message.from_user.id, days=PREMIUM_DURATION_DAYS)
    await message.answer(texts.premium_purchased_text(new_until))

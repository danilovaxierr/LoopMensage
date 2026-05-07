LOOP MENSAGE BOT

1) No BotFather, revogue o token antigo e gere um novo.
2) Copie .env.example para .env e preencha:
   BOT_TOKEN, ADMIN_ID, SYNCPAY_CLIENT_ID, SYNCPAY_CLIENT_SECRET.
3) Instale:
   pip install -r requirements.txt
4) Rode:
   python main.py

OBS: A integração SyncPay está centralizada nas funções syncpay_create_pix() e syncpay_webhook().
Se sua documentação SyncPay usar endpoints ou campos diferentes, mude só essa parte.

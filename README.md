# Finance & Admin – Baccano Partners

App **locale** (gira sul tuo computer, si usa dal browser) per:

- **Finance**: saldi di conti e carte, entrate e uscite previste giorno per giorno, forecast della liquidità con soglia minima, riconciliazione tra movimenti reali e voci previste.
- **Administration**: scadenzario dei documenti da consegnare al commercialista (mensili entro il 6, estratti conto trimestrali dal 15), archivio ordinato dei file, export automatici dai movimenti, pacchetto ZIP + email per l'invio.

## Online su Render (consigliato)

L'app è pronta per essere pubblicata su [Render](https://render.com) con il file `render.yaml`. Ottieni un indirizzo `https://…onrender.com`, protetto da password, apribile da PC e telefono. Ogni modifica caricata su GitHub viene pubblicata da sola.

1. Crea un account su render.com con "Sign in with GitHub" e autorizza l'accesso al repository `FinanceBaccano-Partners`.
2. **New → Blueprint** → scegli il repository → Render legge `render.yaml`.
3. Quando lo chiede, inserisci **APP_PASSWORD**: la password per entrare nell'app. Usane una lunga.
4. Conferma la creazione (piano Starter con disco da 1 GB, circa 7 $/mese). Dopo qualche minuto l'app è attiva all'indirizzo mostrato da Render.

I dati (database e documenti) stanno sul disco persistente `/var/data`, con un backup automatico al giorno. Da *Impostazioni → Scarica backup completo* puoi scaricarne una copia quando vuoi.

## Avvio in locale (alternativa)

Requisito: **Python 3.11+** ([python.org](https://www.python.org/downloads/); su Windows spunta "Add Python to PATH").

- **Windows**: doppio clic su `avvia.bat`
- **macOS**: doppio clic su `avvia.command` (la prima volta: tasto destro → Apri)
- **Linux**: `./avvia.sh`

Al primo avvio viene preparato l'ambiente (1–2 minuti). Poi si apre il browser su <http://127.0.0.1:8765>. Per chiudere l'app: chiudi la finestra del terminale oppure premi CTRL+C.

### Dove finiscono i dati

Tutto sta in `~/FinanceBaccano` (la cartella utente: `C:\Users\<nome>\FinanceBaccano` su Windows):

| Cosa | Dove |
|---|---|
| Database (conti, movimenti, previsti, fatture) | `finance.db` |
| Backup automatico giornaliero (ultimi 30) | `backup/` |
| Documenti per il commercialista, divisi per anno/mese/tipologia | `documenti/` (modificabile in Impostazioni) |
| Pacchetti ZIP mensili | `pacchetti/` (consigliato: una cartella di Google Drive sincronizzata) |

Per spostare l'archivio altrove: variabile d'ambiente `FBP_DATA_DIR`.

### Provarla con dati finti

```bash
FBP_DATA_DIR=~/FinanceBaccano-demo .venv/bin/python scripts/demo.py
FBP_DATA_DIR=~/FinanceBaccano-demo .venv/bin/python run.py
```

## Primi passi

1. **Impostazioni**: ragione sociale, **P.IVA** (serve a distinguere fatture attive e passive), email del commercialista, soglia minima di liquidità, cartella pacchetti su Drive.
2. **Conti e carte**: crea Vivid, Revolut Business, BCC, le carte e una "Cassa contanti", ognuno con il saldo iniziale.
3. **Movimenti → Importa**: carica il CSV o l'Excel esportato dall'home banking. La prima volta controlli il collegamento delle colonne; poi la mappatura viene ricordata. I file sovrapposti non creano doppioni.
4. **Fatture**: trascina gli XML, i P7M o lo ZIP esportati da Aruba. Ogni scadenza di pagamento diventa un'entrata o un'uscita prevista. Al primo import dello storico, indica la data prima della quale le scadenze sono già saldate.
5. **Ricorrenze**: stipendi, F24, affitto, leasing, canoni clienti…
6. **Riconciliazione**: abbina i movimenti importati alle voci previste. Gli abbinamenti certi si fanno con un clic.
7. **Scadenzario documenti**: per ogni mese, carica i documenti per tipologia. Lista movimenti banche, carte e contanti si possono **generare in Excel** dai movimenti. Quando hai finito, apri **Pacchetto commercialista**: lo ZIP viene creato nella cartella Drive, l'email si apre già compilata e lo scadenzario viene segnato come inviato.

## La routine settimanale (10 minuti)

1. Scarica dall'home banking i movimenti di Revolut, Vivid e BCC in CSV o Excel, dall'ultimo caricamento a oggi. Sovrapporre qualche giorno non crea doppioni.
2. Scarica da Aruba le fatture emesse e ricevute (XML, P7M o ZIP).
3. Raccogli PDF e foto di ricevute estere, scontrini, Telepass, assegni ed estratti conto delle carte.
4. In app apri **Caricamento settimanale**, trascina tutto insieme, controlla cosa ha capito l'app e premi **Salva tutto**.

L'app smista ogni file:
- i movimenti vanno sul conto giusto, riconosciuto dal formato del file, dall'IBAN o dal nome del file;
- le scadenze delle fatture finiscono nel forecast;
- i documenti vanno nello scadenzario, con tipologia e mese ricavati dal nome del file;
- i movimenti vengono abbinati in automatico alle voci previste quando la corrispondenza è certa.

Il primo estratto di ogni banca chiede una volta sola quali sono le colonne. Se l'ultimo caricamento è di oltre 7 giorni fa, la dashboard te lo ricorda.

## Open Banking (Enable Banking) – opzionale

Non necessario se usi il caricamento settimanale.

Pagina **Open Banking (opzionale)**: saldi e movimenti si scaricano da soli (PSD2, sola lettura), all'avvio dell'app e poi ogni 6 ore mentre è aperta. Funziona anche il pulsante "Aggiorna dalle banche" in dashboard.

1. Crea un account su [enablebanking.com](https://enablebanking.com) → Control Panel → *API applications* → nuova applicazione **Production**, con redirect URL `http://127.0.0.1:8765/banche/callback`.
2. Il browser scarica la chiave privata `.pem`: caricala in *Banche collegate → Configurazione* insieme all'Application ID.
3. Nel Control Panel collega i tuoi conti all'applicazione. In modalità ristretta l'API vede solo quelli.
4. In app: *Collega una banca* → scegli Revolut, Vivid o la tua BCC → autorizzi sul sito della banca → associ i conti.
5. Ogni 90–180 giorni la banca chiede di rinnovare il consenso: la dashboard avvisa 14 giorni prima, poi basta un clic su "Rinnova consenso".

Se un conto aveva già movimenti importati da file, la sincronizzazione riparte dal giorno successivo all'ultimo movimento: niente doppioni.

## Lettura dei PDF con l'intelligenza artificiale (opzionale)

Quando la lettura automatica di un estratto conto PDF non funziona (impaginazioni particolari, scansioni), l'anteprima dell'import mostra il pulsante **Leggi con l'AI**. Il PDF viene inviato a Claude (Anthropic), che restituisce i movimenti strutturati; tu li controlli in anteprima prima di importarli.

- **Attivazione:** crea una chiave API su [console.anthropic.com](https://console.anthropic.com), poi su Render → servizio → *Environment* aggiungi la variabile `ANTHROPIC_API_KEY`. Il servizio si riavvia da solo.
- **Costo:** si paga a consumo sul tuo account Anthropic. Con il modello predefinito (Claude Opus 5.5) un estratto di qualche pagina costa pochi centesimi, uno da 50+ pagine 1–2 €. L'anteprima mostra il costo stimato di ogni lettura. Il modello si può cambiare con la variabile `FBP_AI_MODEL`.
- **Privacy:** il contenuto del PDF viene elaborato dal servizio di Anthropic. Se non imposti la chiave la funzione resta spenta e nessun dato esce dall'app.

## Analisi spese

Ogni movimento riceve in automatico una categoria (Software e abbonamenti, Carburante e trasporti, Tasse e contributi, Ristoranti e bar…) in base alle parole della descrizione e della controparte. La pagina **Analisi spese** mostra un grafico a barre delle uscite per categoria, con percentuali, dettaglio per fornitore e filtro per periodo e conto. I giroconti tra conti propri e le entrate sono esclusi.

L'app ricava dalla descrizione il nome dell'esercente, togliendo le parti bancarie che non servono (tipo di operazione, carta, data e ora, mandati SDD, riferimenti, circuiti come PayPal/SumUp, città) e riconoscendo i marchi più comuni: "AMZN Mktp IT*1A2B", "AMAZON EU SARL" e "Amazon.it" diventano tutti **Amazon**. Nella pagina Analisi il riquadro **Da classificare** raggruppa per esercente le spese finite in "Altro": una scelta vale per tutto il gruppo e per i movimenti futuri. Con la chiave Anthropic attiva, **Classifica con l'AI** fa proporre a Claude la categoria dei gruppi rimasti, scegliendo solo tra le tue categorie (vengono inviati solo i nomi degli esercenti e una descrizione d'esempio, senza importi).

**Regola base dei bonifici:** i bonifici in uscita verso persone e ditte (es. "Giorgio Cicero", "Renovatio di Cipriani e Boezio") finiscono in **Subappalti e conto terzi**, perché sono operai e ditte che lavorano per noi. Restano nella loro categoria i bonifici riconoscibili come tasse, stipendi, consulenze, utenze, affitto ecc., e i bonifici verso la nostra società sono giroconti. La stessa regola vale per la classificazione con l'AI.

Quando una categoria è sbagliata, correggila in **Movimenti**: l'app crea una regola e la applica a tutti i movimenti dello stesso fornitore, presenti e futuri. Le categorie scelte a mano non vengono mai sovrascritte.

## Come funziona il forecast

- **Saldo di un conto** = ultimo *saldo certo* (inserito a mano, oppure letto dalla colonna saldo del file importato) + movimenti successivi.
- **Saldo previsto di un giorno** = saldo di oggi + voci previste ancora aperte fino a quel giorno. Le voci scadute e non abbinate possono essere conteggiate oggi (opzione nelle Impostazioni).
- Una voce **abbinata** a un movimento esce dal forecast, perché è già nel saldo.
- Si considerano i conti in EUR attivi con "Includi nel forecast". Le carte di credito di solito si escludono e si mette tra i previsti l'addebito mensile sul conto.

## Scelta architetturale: app locale

| | Locale (scelta) | Cloud |
|---|---|---|
| Privacy dei dati bancari | restano sul tuo PC | su un server di terzi |
| Costi | zero | hosting e database |
| Accesso da telefono / fuori ufficio | no (si può aggiungere con una VPN come Tailscale) | sì |
| Aggiornamento automatico dei saldi | solo a PC acceso | anche a PC spento |
| Backup | automatico in locale, meglio se copiato su Drive | gestito dal provider |

Per un solo utente la soluzione locale è più semplice, sicura ed economica. Il codice è una normale web app (FastAPI + SQLite), quindi può essere spostata in cloud in seguito senza riscriverla.

## Roadmap

- [x] **Fase 1**: conti, import CSV/Excel, previsti, ricorrenze, fatture XML Aruba, forecast, riconciliazione, scadenzario, pacchetto commercialista
- [x] **Fase 2 – Open Banking** con Enable Banking: sincronizzazione automatica di saldi e movimenti, rinnovo del consenso, ricollegamento dei conti per IBAN
- [ ] **Fase 3 – Aruba API**: download automatico delle fatture emesse e ricevute, senza export manuale.
- [ ] **Fase 4**: raccolta automatica dalla casella email delle ricevute dei fornitori esteri (Google, AWS, Meta…); lettura OCR di scontrini e ricevute; notifiche desktop delle scadenze; eseguibile installabile (.exe/.app).

## Sviluppo

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/python run.py
```

Struttura: `app/services/` (logica: forecast, import banche, FatturaPA, riconciliazione, scadenzario), `app/routers/` (pagine), `app/templates/` (HTML), `tests/`.

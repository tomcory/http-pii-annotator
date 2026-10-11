| PII Data type                                     | Sensitivity level | Example values                                                                                       |
|---------------------------------------------------|-------------------|------------------------------------------------------------------------------------------------------|
| Name                                              | medium            | `John Doe`, `Amina Khan`, `Wei Zhang`                                                                |
| Email                                             | high              | `john.doe@example.com`, `a.khan@example.org`                                                         |
| Phone number                                      | high              | `+49 30 1234567`, `+1 415 555 0123`                                                                  |
| Credit card number                                | high              | `4111 1111 1111 1111` (test), `5555 5555 5555 4444` (test)                                           |
| Passport number                                   | high              | `X12345678`, `P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<` (MRZ-like)                               |
| Social security number                            | high              | `123-45-6789` (synthetic)                                                                            |
| Date of birth                                     | medium            | `1994-07-16`, `16/07/1994`                                                                           |
| Address                                           | medium            | `123 Example St, Apt 4B, Sample City`, `42 Fictional Road, Unit 12`                                  |
| Zip code                                          | medium            | `10115`, `94105`, `SW1A 1AA`                                                                         |
| IP address                                        | high              | `203.0.113.42`, `198.51.100.17`                                                                      |
| Bank account number                               | high              | `DE89 3704 0044 0532 0130 00` (IBAN-like), `1234567890`                                              |
| Routing number                                    | medium            | `021000021` (synthetic), `011000015` (synthetic)                                                     |
| Driver’s license number                           | medium            | `D123-456-789-012` (synthetic), `S1234567`                                                           |
| License plate number                              | medium            | `B-AB 1234`, `CA7X2K9` (synthetic)                                                                   |
| Vehicle identification number                     | low               | `1HGCM82633A004352` (synthetic), `WVWZZZ1JZXW000001` (synthetic)                                     |
| Bank account details                              | high              | `acct_type=checking; bank=ExampleBank; nickname=Household`, `sort_code=12-34-56; account_name=J Doe` |
| Medical record number                             | high              | `MRN-00492817`, `HOSP-7781203`                                                                       |
| Health insurance number                           | high              | `HIN-AX39-7721`, `INS-9941-2033`                                                                     |
| Employee number                                   | medium            | `EMP-0001734`, `EID-98231`                                                                           |
| Student ID                                        | medium            | `S-2025-019283`, `U12345678`                                                                         |
| Government ID                                     | high              | `ID-GBR-4729910`, `NID-000-992-113`                                                                  |
| Geolocation                                       | high              | `Berlin, DE`, `Kreuzberg`, `geohash=u33dc1` (coarse)                                                 |
| Biometric data                                    | high              | `bio_template_hash=9f2c1a...`, `embedding=[0.12,-0.03,0.44,...]`                                     |
| Mother’s maiden name                              | medium            | `Schmidt`, `Nguyen`                                                                                  |
| Family member names                               | medium            | `Maria Doe`, `Ahmed Khan`                                                                            |
| Place of birth                                    | medium            | `Hamburg, DE`, `Leeds, UK`                                                                           |
| Citizenship                                       | medium            | `DE`, `GB`, `dual: DE/FR`                                                                            |
| Gender                                            | medium            | `female`, `male`, `non-binary`, `prefer_not_to_say`                                                  |
| Sexual orientation                                | medium            | `heterosexual`, `bisexual`, `asexual`, `prefer_not_to_say`                                           |
| Race/ethnicity                                    | medium            | `Asian`, `Black`, `White`, `Hispanic/Latino`, `prefer_not_to_say`                                    |
| Nationality                                       | medium            | `German`, `Canadian`, `Japanese`                                                                     |
| Religious beliefs                                 | medium            | `Christian`, `Muslim`, `Buddhist`, `atheist`, `prefer_not_to_say`                                    |
| Political affiliation                             | medium            | `Green Party`, `Conservative`, `Independent`, `prefer_not_to_say`                                    |
| Professional license number                       | medium            | `MED-PL-883201`, `BAR-102938`, `PE-774400`                                                           |
| Education history                                 | medium            | `BSc Computer Science (2018)`, `TU Berlin—MSc (2023)`                                                |
| Employment history                                | medium            | `Data Analyst at ExampleCo (2021–2024)`, `Intern—LabTech (2019)`                                     |
| Income level                                      | low               | `€40k–€60k`, `income_bracket=3`, `high`                                                              |
| Financial status                                  | low               | `debt_to_income=0.32`, `assets_band=mid`, `credit_utilisation=18%`                                   |
| Social media handles                              | low               | `@john_doe`, `@amina.k`, `@wei-zhang`                                                                |
| Account numbers                                   | high              | `acct_9834012`, `customer_no=00077123`, `account_id=a1b2c3d4`                                        |
| Transaction history                               | high              | `txn_2026-02-17_00019: €29.99`, `merchant=ShopNest; amount=12.40; ts=2026-02-01`                     |
| Digital signature                                 | high              | `sig=MEUCIQDc...` (base64), `ed25519:3a9f...`                                                        |
| Purchase history                                  | medium            | `order_100021: “Running shoes”`, `purchases=[{"sku":"A12","qty":1}]`                                 |
| Subscription information                          | medium            | `plan=premium_monthly`, `renewal=2026-03-01`, `status=active`                                        |
| Health information (e.g., conditions, treatments) | high              | `condition=asthma`, `treatment=physiotherapy`, `medication=ibuprofen`                                |
| Emergency contact information                     | low               | `Emma Doe, +49 30 7654321`, `contact=Ali Khan; relation=spouse; phone=+1 415 555 0199`               |
| Insurance policy number                           | high              | `POL-EX-2026-000771`, `AUTO-INS-8839201`                                                             |
| Academic records                                  | medium            | `GPA=3.7`, `grades={"BIO101":"A","CHEM102":"B+"}`                                                    |
| Tax identification number                         | low               | `TIN-992-11-3001`, `DE-TaxID-12345678901` (synthetic)                                                |
| License key or serial number                      | medium            | `AAAA-BBBB-CCCC-DDDD`, `SN-EX-2026-001992`                                                           |
| Location data (GPS)                               | high              | `52.5200, 13.4050`, `lat=48.8566&lon=2.3522`                                                         |
You extract facts from one remote job posting for one candidate. Do NOT compute a score. Output JSON only:
{
 "eligible_from_finland": true|false,      // false if US-only, must-be-based-in list without Finland/EU, non-EU payroll only
 "language_blocker": true|false,           // a language the candidate does not have at the needed level (see CANDIDATE) is REQUIRED (not preferred)
 "timezone_blocker": true|false,           // working hours required in a non-European timezone
 "clearance_or_defence": true|false,
 "seniority_title": true|false,            // Senior/Staff/Principal/Lead/Head/Director/VP in title or body
 "years_required": "exact phrase or null",
 "years_required_num": integer|null,       // the minimum number in that phrase
 "years_scope": "pm_title"|"qa_dev"|"total"|null,  // pm_title = years in PM/PO/TPM/CS/TAM title; qa_dev = years in QA/dev/engineering; total = general professional experience
 "role_family": "product"|"technical_pm"|"qa"|"support_cs"|"solutions_implementation"|"business_analyst"|"engineering"|"other",
 "requirements_total": integer,            // count of listed hard requirements (skills, tools, years, domain)
 "requirements_met": integer,              // how many the candidate meets from CANDIDATE below; be strict, a tool the candidate has not used is not met
 "domain_required": true|false,            // domain knowledge listed as required (medical, pharma, banking compliance, mobile B2C, crypto, etc.)
 "outsourcing_employer": true|false,       // staffing agency, BPO, dev shop, body-leasing
 "salary": "as shown or null",
 "salary_eur_month": integer|null,         // rough conversion of the LOWER bound to EUR per month
 "language": "en"|"de"|"other",            // language of the posting
 "misleading_title": true|false,           // body describes PO/PM/QA/support work under another title
 "residency_restriction": true|false,      // must reside in specific non-EU states/countries, or clearances tied to one country (e.g. US state list, FBI check)
 "company_country": "string or null",      // where the company is founded/headquartered, from the posting text only; null if not stated
 "local_market_product": true|false,       // product exists only for one non-EU national market (e.g. Indian Aadhaar/RBI, US state homeschool, UAE banking)
 "religious_or_political": true|false,     // religious, church, or political mission statements in the posting
 "posting_date": "YYYY-MM-DD or YYYY-MM or null",  // any posted/published date visible in the text; do not guess
 "reason": "one sentence: the 2–3 facts that decide this one"
}

CANDIDATE
{{CANDIDATE_FACTS}}

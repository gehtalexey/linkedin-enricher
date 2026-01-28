---
name: email-subject-line
description: Generate email subject line and personal note for candidate outreach. Use when writing recruiting emails to candidates.
argument-hint: [candidate-profile-info]
---

Generate TWO things for a recruiting email:
1. **Subject line** - under 10 words
2. **Personal note** - 1-2 sentences for follow-up email body

## Context
- Email is from a hiring manager (team leader, VP R&D, etc.)
- Target audience: candidates in Israel
- Subject line for initial outreach, personal note for follow-up body

## ONLY use these reliable data points:
1. **Previous employer** (job_2 company name)
2. **Education** (school name)
3. **Current title** (Team Lead, Staff, Founding, etc.)
4. **Interesting skills** (Rust, Go, K8s, Kafka, Scala, Elasticsearch, etc.)

## DO NOT use interpreted/potentially wrong data:
- Years at company (could be outdated)
- Specific achievements or projects
- Anything that sounds like it was summarized or interpreted
- Tenure claims ("5+ years")

## CRITICAL VALIDATION RULES (check before output):
1. **No em dashes** - Never use "—" anywhere
2. **No duplicate companies** - If prev company and current company are the same (or variants like "Pecan" and "Pecan AI", "Waze" and "Google/Waze", "Bionic" and "CrowdStrike/Bionic"), do NOT use company transition format. Pick education, title, or skills instead.
3. **Clean company names** - Strip suffixes: Ltd, Inc, Corp, Technologies, Labs, .io, Group, Solutions. Use core brand only:
   - "Healthy.io" -> "Healthy"
   - "AI21 Labs" -> "AI21"
   - "GSI Technology" -> "GSI"
   - "NSO Group" -> "NSO"
   - "Argus Cyber Security Ltd." -> "Argus"
   - "Istra Research Ltd." -> "Istra Research"
4. **Same parent company = same company** - Google/Waze, Unity/ironSource, SAP/Gigya are same company. Don't use "[A] to [B]?" format.

## Priority order for subject line:
1. Notable previous company (Google, Meta, Waze, Snyk, JFrog, etc.) - ONLY if different from current
2. Notable school (Technion, TAU, Hebrew U, BGU, Weizmann)
3. Interesting title (Team Lead, Staff, Founding Engineer, Architect)
4. Any previous company (fallback) - ONLY if different from current
5. Interesting skill (Rust, Go, K8s, Kafka, Scala)
6. Generic based on current company

## NEVER use (instant disqualify):
- Em dashes (—)
- "I hope this message finds you well"
- "I came across your profile"
- "I was impressed by"
- "I noticed"
- "Reaching out because"
- Any generic opener that could apply to anyone

## Good examples

Subject lines (based on prev company):
- "Snyk to Wilco?"
- "Vimeo to Ultima Genomics?"
- "Google to Foretellix?"
- "Facebook to Apiiro?"
- "VMware to Orca Security?"
- "Salesforce to Avalor?"
- "Apple to Habana Labs?"
- "Amazon to Regatta?"

Subject lines (based on education):
- "Technion grad building at [company]?"
- "Technion grad at [company]?"
- "TAU CS at [company]?"
- "TAU MSc at [company]?"
- "TAU Math+CS at [company]?"
- "Hebrew U grad at [company]?"
- "BGU engineer at [company]?"
- "Haifa University grad at [company]?"
- "Weizmann background at [company]?"
- "Bar-Ilan grad at [company]?"

Subject lines (based on title):
- "Still hands-on as lead?"
- "Tech Lead at [company]?"
- "Staff engineer at [company]?"
- "Founding engineer at [company]?"
- "First employee at [company]?"
- "From manager back to IC?"
- "R&D Lead at [company]?"
- "CTO to engineer at [company]?"

Subject lines (based on skills):
- "Rust in production?"
- "Go and K8s at scale?"
- "Go at [company]?"
- "Kafka at [company]?"
- "Serverless at [company]?"
- "GraphQL at [company]?"
- "Scala at [company]?"
- "FastAPI at [company]?"
- "Vector search at [company]?"

## Personal note rules:
1. **Pick a DIFFERENT angle** than subject line used
2. **Be specific** - reference something concrete from their profile
3. **Vary the format** - don't always use the same template
4. **Match the tone to fit level** - strong fit gets more enthusiastic, good fit more curious

## Personal note variety (rotate through these styles):

**For company transitions:**
- "Big move from [prev]. What pulled you over?"
- "[Prev] to [current] is an interesting jump. What sparked the change?"
- "Curious what drew you from [prev]."
- "[Prev] to [current] - what's the biggest change?"
- "Two strong [domain] companies back to back. What draws you to the space?"

**For education:**
- "Technion is no joke. How do you apply that rigor day-to-day?"
- "TAU CS is competitive. What area grabbed your interest?"
- "Hebrew U has strong CS. What got you into backend?"
- "BGU engineering is solid. What kind of problems do you enjoy solving?"
- "BGU with honors is impressive. What area of CS grabbed your interest most?"
- "[School] MSc is a strong combo. How do you apply both?"

**For titles/leadership:**
- "Leading and coding is a balancing act. How do you split your time?"
- "Curious how you keep shipping code while managing a team."
- "Team lead who still builds - that's rare. How do you make time?"
- "Tech Lead who still codes - how do you balance both responsibilities?"

**For skills:**
- "Rust in production is still rare. How's adoption going with the team?"
- "K8s at scale gets complex. What's your approach?"
- "Go for backend is a solid choice. What sold you on it?"
- "Kafka can be tricky. What's your setup like?"
- "Go and Python together is a solid combo. Which do you prefer for backend?"
- "Distributed systems are tricky. What's the hardest part?"
- "NestJS and TypeScript is a solid combo. How's the team using it?"
- "Spark and Airflow at scale is tricky. What's your approach?"

**For interesting career paths:**
- "First employee at a startup is intense. What was the wildest part?"
- "Manager back to IC is a bold move. What drove that decision?"
- "Building from scratch is a different game. What do you enjoy most?"
- "CTO to engineer is interesting. What do you enjoy more about hands-on work?"
- "7+ years at one company is rare. What keeps you engaged?"
- "Research to engineering is an interesting path. How does that help you?"

**For acquisitions/transitions:**
- "[Company] to [Acquirer] acquisition is a ride. How's the transition going?"
- "7+ years through an acquisition - how did [Acquirer] change things?"

**For unique backgrounds:**
- "[Non-CS field] to engineering is unique. How does that background help?"
- "Physics to software architecture is interesting. How does that background help?"
- "Publishing tech articles while building is impressive. What do you write about?"

## Input
Candidate profile information: $ARGUMENTS

## Output
Return in this exact format:
Subject: [subject line here]
Note: [personal note here]

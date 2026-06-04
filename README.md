Games Website Project - README

This project is a web application designed to facilitate various games and quizzes, providing an engaging and interactive platform for users. Below, you will find detailed descriptions of the features and tools used in building this application.

## Games and Features
1. **Who is That?**
   - Participants answer questions about identifying people from images.
   - Supports a variety of topics.

2. **Who is Lying?**
   - Players attempt to identify who is lying among a set of people based on a statement.
   - Includes features for managing participants, tracking scores, and more.

3. **Where is This?**
   - Users determine the geographical location by identifying landmarks or places from images.
   - Offers customizable question sets.

4. **Sorting Ladder**
   - Participants sort items according to a predefined order (e.g., countries by size).
   - Tracks progress and scores across multiple rounds.

5. **Clue Rush**
   - A trivia quiz with questions and interactive timed clues.

6. **Black Jack Quiz**
   - Engage in a numerical-based trivia quiz modeled after the popular game BlackJack.
   - Includes features to track points and determine winners based on answers.

7. **Quick Quiz**
   - A versatile quiz system that can handle various types of questions including multiple choice, true/false, and short answer.
   - Allows for customizable quizzes and tracks scores across participants.

8. **Estimation**
   - Participants estimate numerical values (e.g., population sizes, prices) to test their knowledge.
   - Provides feedback on accuracy and detailed statistics.

9. **Assign**
   - A drag-and-drop game where users match items from one set to another (e.g., matching countries with capitals).
   - Tracks progress and scores across multiple rounds.

## Local Browser/E2E Tests

Some Hub and admin-dashboard tests use Playwright with Django LiveServer/Channels
to simulate host and participant browser flows.

Install the Python dependencies and Chromium browser locally:

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

Run the focused Hub check-in E2E test:

```powershell
python manage.py test games_hub.test_check_in_e2e
```

If Playwright or Chromium is missing, the browser tests skip themselves instead
of failing the full Django test suite. They run actively as soon as both are
installed.

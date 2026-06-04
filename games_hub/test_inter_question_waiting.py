from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent


def read_template(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def extract_section(content: str, start: str, end: str) -> str:
    return content.split(start, 1)[1].split(end, 1)[0]


def assert_any_in(testcase, haystack: str, snippets, message: str):
    testcase.assertTrue(
        any(snippet in haystack for snippet in snippets),
        msg=message,
    )


class InterQuestionWaitingSourceTests(unittest.TestCase):
    def test_question_end_and_reveal_paths_do_not_switch_to_waiting_states(self):
        expectations = [
            (
                "templates/quiz/play.html",
                [
                    ("onQuestionEnded(data) {", "onQuizEnded(data) {", ["this.showWaitingForNextQuestion();"]),
                    ("onTimeUp() {", "showWaitingForNextQuestion() {", ["this.showWaitingForNextQuestion();"]),
                ],
            ),
            (
                "templates/estimation/play.html",
                [
                    ("onTimeUp() {", "showCorrectAnswer(correctAnswerData, pointsForQuestion = 0, rankResults = null) {", ["this.showWaitingForNextQuestion();"]),
                    ("showCorrectAnswer(correctAnswerData, pointsForQuestion = 0, rankResults = null) {", "formatRevealPercentage(value) {", ["this.showWaitingForNextQuestion();"]),
                ],
            ),
            (
                "templates/who_is_lying/play.html",
                [
                    ("onQuestionEnded() {", "onQuizEnded(data) {", ["this.showWaitingForNextQuestion();"]),
                ],
            ),
            (
                "templates/who_is_that/play.html",
                [],
            ),
            (
                "templates/sorting_ladder/play.html",
                [
                    ("onQuestionEnded(data) {", "showCorrectAnswer(correctAnswerData) {", ["this.showWaitingForNextQuestion();"]),
                    ("showCorrectAnswer(correctAnswerData) {", "showWaitingForNextQuestion() {", ["this.showWaitingForNextQuestion();"]),
                ],
            ),
            (
                "templates/where_is_this/play.html",
                [
                    ("onQuestionEnded() {", "onQuizEnded(data) {", ["this.showWaitingForNextQuestion();"]),
                    ("onTimeUp() {", "showWaitingForNextQuestion() {", ["this.showWaitingForNextQuestion();"]),
                ],
            ),
            (
                "templates/clue_rush/play.html",
                [
                    ("onQuestionEnded(data) {", "resetClues() {", ["this.showWaitingForNextQuestion();"]),
                    ("showCorrectAnswer(correctAnswerData) {", "init() {", ["this.showWaitingForNextQuestion();"]),
                ],
            ),
            (
                "templates/assign/play.html",
                [
                    ("onQuestionEnded() {", "onQuizEnded(data) {", ["this.showWaitingForNextQuestion();"]),
                    ("onQuestionRoundsComplete(data = {}) {", "onShowSolution(data) {", ["this.showState('waitingSolutionState');"]),
                ],
            ),
            (
                "templates/black_jack_quiz/play.html",
                [
                    ("onQuestionEnded(data) {", "onQuizEnded(data) {", ["this.completeQuestionTransition(data);"]),
                    ("onTimeUp() {", "requestQuestionTimeoutSync() {", ["this.showWaitingForNextQuestion();"]),
                    ("showCorrectAnswer(correctAnswerData) {", "completeQuestionTransition(data) {", ["this.completeQuestionTransition(this.lastQuestionEndData || {});"]),
                    ("completeQuestionTransition(data) {", "resetForNextSet(nextSetNumber) {", ["this.showWaitingForNextQuestion();", "this.showState('quizEndedState');"]),
                ],
            ),
        ]

        for relative_path, sections in expectations:
            content = read_template(relative_path)
            if relative_path == "templates/who_is_that/play.html":
                self.assertNotIn("Waiting for Next Question", content)
                self.assertNotIn("showWaitingForNextQuestion", content)
                continue

            for start, end, forbidden_snippets in sections:
                section = extract_section(content, start, end)
                for snippet in forbidden_snippets:
                    self.assertNotIn(snippet, section, msg=f"{relative_path}: found forbidden snippet in section {start}")

    def test_next_question_entry_points_and_reveals_remain_defined(self):
        checks = [
            ("templates/quiz/play.html", "onQuestionStarted(question, timeLimit) {", "this.showState('questionState');"),
            ("templates/estimation/play.html", "onQuestionStarted(question) {", "this.showState('questionState');"),
            ("templates/who_is_lying/play.html", "onQuestionStarted(question) {", "this.showState('questionState');"),
            ("templates/who_is_that/play.html", "onQuestionStarted(question) {", ["this.showState('questionState');", "this.showState(submittedState ? 'answerSubmittedState' : 'questionState');"]),
            ("templates/sorting_ladder/play.html", "onQuestionEnded(data) {", "this.showState('finalOrderState');"),
            ("templates/where_is_this/play.html", "onQuestionStarted(question) {", "this.showState('questionState');"),
            ("templates/clue_rush/play.html", "showCorrectAnswer(correctAnswerData) {", "this.showState('correctAnswerState');"),
            ("templates/assign/play.html", "onShowSolution(data) {", "this.showState('solutionState');"),
            ("templates/black_jack_quiz/play.html", "onQuestionStarted(question) {", "this.showState('questionState');"),
        ]

        for relative_path, section_start, expected_snippet in checks:
            content = read_template(relative_path)
            self.assertIn(section_start, content, msg=f"{relative_path}: missing section {section_start}")
            section = content.split(section_start, 1)[1]
            if isinstance(expected_snippet, list):
                assert_any_in(
                    self,
                    section,
                    expected_snippet,
                    f"{relative_path}: missing expected snippet after {section_start}",
                )
            else:
                self.assertIn(expected_snippet, section, msg=f"{relative_path}: missing expected snippet after {section_start}")

    def test_host_end_redirect_to_lobby_is_preserved(self):
        templates = [
            "templates/quiz/play.html",
            "templates/estimation/play.html",
            "templates/who_is_lying/play.html",
            "templates/who_is_that/play.html",
            "templates/sorting_ladder/play.html",
            "templates/where_is_this/play.html",
            "templates/clue_rush/play.html",
            "templates/assign/play.html",
            "templates/black_jack_quiz/play.html",
        ]

        for relative_path in templates:
            content = read_template(relative_path)
            self.assertIn("onQuizEnded(data) {", content, msg=f"{relative_path}: missing quiz end handler")
            self.assertIn("createHubLobbyReturnController", content, msg=f"{relative_path}: missing lobby return controller")
            self.assertIn("/hub/lobby/", content, msg=f"{relative_path}: missing lobby redirect path")


if __name__ == "__main__":
    unittest.main()

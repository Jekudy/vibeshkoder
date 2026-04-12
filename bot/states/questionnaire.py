from aiogram.fsm.state import State, StatesGroup


class QuestionnaireForm(StatesGroup):
    q1_name = State()
    q2_location = State()
    q3_source = State()
    q4_experience = State()
    q5_projects = State()
    q6_hardest = State()
    q7_goals = State()
    confirm = State()


STATES_LIST = [
    QuestionnaireForm.q1_name,
    QuestionnaireForm.q2_location,
    QuestionnaireForm.q3_source,
    QuestionnaireForm.q4_experience,
    QuestionnaireForm.q5_projects,
    QuestionnaireForm.q6_hardest,
    QuestionnaireForm.q7_goals,
]

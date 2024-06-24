import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TypedDict

from django.db import transaction
from django.utils.timezone import now

from evap.evaluation.models import Contribution, Course, CourseType, Degree, Evaluation, Semester, UserProfile
from evap.evaluation.tools import clean_email
from evap.staff.tools import update_or_create_with_changes, update_with_changes

logger = logging.getLogger("import")


class ImportStudent(TypedDict):
    gguid: str
    email: str
    name: str
    christianname: str


class ImportLecturer(TypedDict):
    gguid: str
    email: str
    name: str
    christianname: str
    titlefront: str


class ImportCourse(TypedDict):
    cprid: str
    scale: str


class ImportRelated(TypedDict):
    gguid: str


class ImportAppointment(TypedDict):
    begin: str
    end: str


class ImportEvent(TypedDict):
    gguid: str
    lvnr: int
    title: str
    title_en: str
    type: str
    isexam: bool
    courses: list[ImportCourse]
    relatedevents: ImportRelated
    appointments: list[ImportAppointment]
    lecturers: list[ImportRelated]
    students: list[ImportRelated]


class ImportDict(TypedDict):
    students: list[ImportStudent]
    lecturers: list[ImportLecturer]
    events: list[ImportEvent]


@dataclass
class NameChange:
    old_last_name: str
    old_first_name_given: str
    new_last_name: str
    new_first_name_given: str


@dataclass
class ImportStatistics:
    name_changes: list[NameChange] = field(default_factory=list)
    new_courses: list[Course] = field(default_factory=list)
    new_evaluations: list[Evaluation] = field(default_factory=list)
    updated_courses: list[Course] = field(default_factory=list)
    updated_evaluations: list[Evaluation] = field(default_factory=list)
    attempted_changes: list[Evaluation] = field(default_factory=list)

    @staticmethod
    def _make_heading(heading: str) -> str:
        heading += "\n" + "".join(["-" for i in heading]) + "\n"
        return heading

    @staticmethod
    def _make_total(total: int) -> str:
        return f"({total} in total)\n\n"

    def get_log(self) -> str:
        log = "JSON IMPORTER REPORT\n"
        log += "====================\n\n"
        log += f"Import finished at {now()}\n\n"
        log += self._make_heading("Name Changes")
        for name_change in self.name_changes:
            log += f"- {name_change.old_first_name_given} {name_change.old_last_name} → {name_change.new_first_name_given} {name_change.new_last_name}\n"
        log += self._make_total(len(self.name_changes))

        log += self._make_heading("New Courses")
        for new_course in self.new_courses:
            log += f"- {new_course}\n"
        log += self._make_total(len(self.new_courses))

        log += self._make_heading("New Evaluations")
        for new_evaluation in self.new_evaluations:
            log += f"- {new_evaluation}\n"
        log += self._make_total(len(self.new_evaluations))

        log += self._make_heading("Updated Courses")
        for updated_course in self.updated_courses:
            log += f"- {updated_course}\n"
        log += self._make_total(len(self.updated_courses))

        log += self._make_heading("Updated Evaluations")
        for updated_evaluation in self.updated_evaluations:
            log += f"- {updated_evaluation}\n"
        log += self._make_total(len(self.updated_evaluations))

        log += self._make_heading("Attempted Changes")
        for attempted_change in self.attempted_changes:
            log += f"- {attempted_change}\n"
        log += self._make_total(len(self.attempted_changes))

        return log


class JSONImporter:
    DATETIME_FORMAT = "%d.%m.%Y %H:%M"

    def __init__(self, semester: Semester) -> None:
        self.semester = semester
        self.user_profile_map: dict[str, UserProfile] = {}
        self.course_type_cache: dict[str, CourseType] = {}
        self.degree_cache: dict[str, Degree] = {}
        self.course_map: dict[str, Course] = {}
        self.statistics = ImportStatistics()

    def _get_course_type(self, name: str) -> CourseType:
        if name in self.course_type_cache:
            return self.course_type_cache[name]

        course_type = CourseType.objects.get_or_create(name_de=name, defaults={"name_en": name})[0]
        self.course_type_cache[name] = course_type
        return course_type

    def _get_degree(self, name: str) -> Degree:
        if name in self.degree_cache:
            return self.degree_cache[name]

        degree = Degree.objects.get_or_create(name_de=name, defaults={"name_en": name})[0]
        self.degree_cache[name] = degree
        return degree

    def _get_user_profiles(self, data: list[ImportRelated]) -> list[UserProfile]:
        return [self.user_profile_map[related["gguid"]] for related in data]

    def _import_students(self, data: list[ImportStudent]) -> None:
        for entry in data:
            email = clean_email(entry["email"])
            user_profile, __, changes = update_or_create_with_changes(
                UserProfile,
                email=email,
                defaults={"last_name": entry["name"], "first_name_given": entry["christianname"]},
            )
            user_profile: UserProfile
            if changes:
                change = NameChange(
                    old_last_name=changes["last_name"][0] if changes.get("last_name") else user_profile.last_name,
                    old_first_name_given=(
                        changes["first_name_given"][0]
                        if changes.get("first_name_given")
                        else user_profile.first_name_given
                    ),
                    new_last_name=changes["last_name"][1] if changes.get("last_name") else user_profile.last_name,
                    new_first_name_given=(
                        changes["first_name_given"][1]
                        if changes.get("first_name_given")
                        else user_profile.first_name_given
                    ),
                )
                self.statistics.name_changes.append(change)

            self.user_profile_map[entry["gguid"]] = user_profile

    def _import_lecturers(self, data: list[ImportLecturer]) -> None:
        for entry in data:
            email = clean_email(entry["email"])
            user_profile, __ = UserProfile.objects.update_or_create(
                email=email,
                defaults={
                    "last_name": entry["name"],
                    "first_name_given": entry["christianname"],
                    "title": entry["titlefront"],
                },
            )

            self.user_profile_map[entry["gguid"]] = user_profile

    def _import_course(self, data: ImportEvent) -> Course:
        course_type = self._get_course_type(data["type"])
        degrees = [self._get_degree(c["cprid"]) for c in data["courses"]]
        responsibles = self._get_user_profiles(data["lecturers"])
        course, created, changes = update_or_create_with_changes(
            Course,
            semester=self.semester,
            cms_id=data["gguid"],
            defaults={"name_de": data["title"], "name_en": data["title_en"], "type": course_type},
        )
        course: Course
        course.degrees.set(degrees)
        course.responsibles.set(responsibles)

        if changes:
            self.statistics.updated_courses.append(course)
        if created:
            self.statistics.new_courses.append(course)

        self.course_map[data["gguid"]] = course

        return course

    # pylint: disable=too-many-locals
    def _import_evaluation(self, course: Course, data: ImportEvent) -> Evaluation:
        course_end = datetime.strptime(data["appointments"][0]["end"], self.DATETIME_FORMAT)

        if data["isexam"]:
            # Set evaluation time frame of three days for exam evaluations:
            evaluation_start_datetime = course_end.replace(hour=8, minute=0) + timedelta(days=1)
            evaluation_end_date = (course_end + timedelta(days=3)).date()

            name_de = "Klausur"
            name_en = "Exam"
        else:
            # Set evaluation time frame of two weeks for normal evaluations:
            # Start datetime is at 8:00 am on the monday in the week before the event ends
            evaluation_start_datetime = course_end.replace(hour=8, minute=0) - timedelta(
                weeks=1, days=course_end.weekday()
            )
            # End date is on the sunday in the week the event ends
            evaluation_end_date = (course_end + timedelta(days=6 - course_end.weekday())).date()

            name_de, name_en = "", ""

        # If events are graded for any degree, wait for grade upload before publishing
        wait_for_grade_upload_before_publishing = any(filter(lambda grade: grade["scale"], data["courses"]))

        participants = self._get_user_profiles(data["students"])

        defaults = {
            "name_de": name_de,
            "name_en": name_en,
            "vote_start_datetime": evaluation_start_datetime,
            "vote_end_date": evaluation_end_date,
            "wait_for_grade_upload_before_publishing": wait_for_grade_upload_before_publishing,
        }
        evaluation, created = Evaluation.objects.get_or_create(
            course=course,
            cms_id=data["gguid"],
            defaults=defaults,
        )
        if evaluation.state < Evaluation.State.APPROVED:
            direct_changes = update_with_changes(evaluation, defaults)

            participant_changes = set(evaluation.participants.all()) != set(participants)
            evaluation.participants.set(participants)

            lecturers_changes = False
            for lecturer in data["lecturers"]:
                __, lecturer_created, lecturer_changes = self._import_contribution(evaluation, lecturer)
                if lecturer_changes or lecturer_created:
                    lecturers_changes = True

            if direct_changes or participant_changes or lecturers_changes:
                self.statistics.updated_evaluations.append(evaluation)
        else:
            self.statistics.attempted_changes.append(evaluation)

        if created:
            self.statistics.new_evaluations.append(evaluation)

        return evaluation

    def _import_contribution(
        self, evaluation: Evaluation, data: ImportRelated
    ) -> tuple[Contribution, bool, dict[str, tuple[any, any]]]:
        user_profile = self.user_profile_map[data["gguid"]]

        contribution, created, changes = update_or_create_with_changes(
            Contribution,
            evaluation=evaluation,
            contributor=user_profile,
        )
        return contribution, created, changes

    def _import_events(self, data: list[ImportEvent]) -> None:
        # Divide in two lists so corresponding courses are imported before their exams
        normal_events = (event for event in data if not event["isexam"])
        exam_events = (event for event in data if event["isexam"])

        for event in normal_events:
            course = self._import_course(event)

            self._import_evaluation(course, event)

        for event in exam_events:
            course = self.course_map[event["relatedevents"]["gguid"]]

            self._import_evaluation(course, event)

    def _process_log(self) -> None:
        log = self.statistics.get_log()
        logger.info(log)

    @transaction.atomic
    def import_dict(self, data: ImportDict) -> None:
        self._import_students(data["students"])
        self._import_lecturers(data["lecturers"])
        self._import_events(data["events"])
        self._process_log()

    def import_json(self, data: str) -> None:
        data = json.loads(data)
        self.import_dict(data)
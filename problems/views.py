import docker
import json
import time
import requests
import re
import platform
import os
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.db.models import Count, Case, When, IntegerField, Q
from .models import Problem, Tag, Solution, TestCase, ProblemRating, FavoriteProblem, Profile
from .forms import ProblemForm, TestCaseFormSet, ProfileForm

def run_code_in_docker(code, test_cases, input_vars, language='python'):
    results = []
    try:
        if platform.system() == 'Windows':
            base_url = 'npipe:////./pipe/docker_engine'
        else:
            base_url = os.environ.get('DOCKER_HOST', 'unix:///var/run/docker.sock')
        
        client = docker.DockerClient(base_url=base_url, timeout=10)
        
        try:
            client.ping()
            print("Docker daemon connection successful")
        except docker.errors.APIError as e:
            print(f"Failed to connect to Docker daemon: {str(e)}")
            return [{'error': 'Cannot connect to Docker service. Please ensure Docker is running.'}]

        type_converters = {
            'int': int,
            'float': float,
            'str': str,
            'bool': lambda x: str(x).lower() == 'true',
            'list': json.loads,
            'dict': json.loads,
            'None': lambda x: None if str(x).lower() == 'null' else x
        }
        type_checks = {
            'int': int,
            'float': float,
            'str': str,
            'bool': bool,
            'list': list,
            'dict': dict,
            'None': type(None)
        }

        for test_case in test_cases:
            input_json = test_case.input_value
            expected_output = test_case.expected_output

            input_dict = json.loads(input_json)
            for var in input_vars:
                if var['name'] in input_dict:
                    value = input_dict[var['name']]
                    expected_type = type_checks.get(var['type'], str)
                    converter = type_converters.get(var['type'], str)
                    if not isinstance(value, expected_type):
                        try:
                            input_dict[var['name']] = converter(value)
                        except (ValueError, json.JSONDecodeError, TypeError):
                            input_dict[var['name']] = value

            input_data_str = "{"
            for key, value in input_dict.items():
                if isinstance(value, bool):
                    input_data_str += f'"{key}": {"True" if value else "False"}, '
                elif value is None:
                    input_data_str += f'"{key}": None, '
                elif isinstance(value, (list, dict)):
                    input_data_str += f'"{key}": {json.dumps(value)}, '
                else:
                    input_data_str += f'"{key}": {repr(value)}, '
            input_data_str = input_data_str.rstrip(", ") + "}"

            wrapper_code = (
                "import json\n"
                "import sys\n\n"
                f"{code}\n\n"
                f"input_data = {input_data_str}\n"
                "print(\"Parameters: \" + str(input_data))\n"
                "result = solution(**input_data)\n"
                "print(\"RESULT_SEPARATOR:\" + json.dumps(result))"
            )
            print(f"Running wrapper code:\n{wrapper_code}")
            try:
                container = client.containers.create(
                    image='python:3.9-slim',
                    command='python -c "{}"'.format(wrapper_code.replace('"', '\\"')),
                    mem_limit='128m',
                    cpu_quota=10000,
                    network_disabled=True,
                    working_dir='/tmp',
                )
                container.start()
                result = container.wait(timeout=5)
                logs = container.logs(stdout=True, stderr=True).decode().strip()
                container.remove()

                log_lines = logs.split('\n')
                console_logs = []
                actual_output_raw = ""

                for line in log_lines:
                    if line.startswith("RESULT_SEPARATOR:"):
                        actual_output_raw = line.replace("RESULT_SEPARATOR:", "").strip()
                    else:
                        console_logs.append(line)

                try:
                    actual_output = json.loads(actual_output_raw)
                except json.JSONDecodeError:
                    actual_output = actual_output_raw

                console_logs_str = "\n".join(console_logs).strip() if console_logs else "No console output"
                print(f"Container logs:\n{logs}")

                return_type = test_case.__dict__.get('return_type', 'str')
                converter = type_converters.get(return_type, str)
                try:
                    expected_parsed = expected_output if isinstance(expected_output, (list, dict)) else json.loads(expected_output)
                except (json.JSONDecodeError, TypeError):
                    expected_parsed = expected_output

                if return_type == 'None':
                    expected_parsed = None if expected_output in [None, 'null', ''] else expected_output
                    actual_output = None if actual_output in [None, ''] else actual_output
                elif return_type == 'str':
                    actual_output = str(actual_output)
                elif return_type in ['int', 'float', 'bool']:
                    actual_output = converter(actual_output) if isinstance(actual_output, (str, int, float, bool)) else actual_output

                passed = actual_output == expected_parsed
                print(f"Debug: actual_output={actual_output} (type={type(actual_output)}), expected_parsed={expected_parsed} (type={type(expected_parsed)}), passed={passed}, return_type={return_type}")

                results.append({
                    'input': input_json,
                    'expected': json.dumps(expected_output) if isinstance(expected_output, (list, dict)) else str(expected_output),
                    'actual': actual_output_raw,
                    'passed': passed,
                    'console_logs': console_logs_str
                })
            except docker.errors.ContainerError as e:
                results.append({'error': f"Container error: {str(e)}"})
            except docker.errors.APIError as e:
                results.append({'error': f"Execution timed out or failed: {str(e)}"})
            except Exception as e:
                results.append({'error': f"Unexpected error: {str(e)}"})
    except Exception as e:
        print(f"Docker client initialization failed: {str(e)}")
        results.append({'error': f"Failed to initialize Docker client: {str(e)}"})

    return results

def generate_function_header(input_vars, return_type):
    params = [f"{var['name']}: {var['type']}" for var in input_vars if var['name'] and var['type']]
    return f"def solution({', '.join(params)}) -> {return_type}:\n"

def signup(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect('problem_list')
    else:
        form = UserCreationForm()
    return render(request, 'signup.html', {'form': form})

def problem_list(request):
    search_query = request.GET.get('q', '')
    selected_tags = request.GET.get('tags', '').split(',') if request.GET.get('tags') else []
    selected_tags = [tag for tag in selected_tags if tag]
    liked = request.GET.get('liked') == 'true'
    disliked = request.GET.get('disliked') == 'true'
    favorited = request.GET.get('favorited') == 'true'

    problems = Problem.objects.all().annotate(
        likes=Count('ratings', filter=Q(ratings__vote=1)),
        dislikes=Count('ratings', filter=Q(ratings__vote=-1))
    ).distinct()

    # Apply search query filter
    if search_query:
        problems = problems.filter(title__icontains=search_query)

    # Apply tag filter
    if selected_tags:
        for tag in selected_tags:
            problems = problems.filter(tags__name=tag)

    # Apply liked/disliked/favorited filters for authenticated users
    if request.user.is_authenticated:
        if liked:
            problems = problems.filter(ratings__user=request.user, ratings__vote=1)
            print(f"Liked filter applied. Problems: {problems.count()}")
        if disliked:
            problems = problems.filter(ratings__user=request.user, ratings__vote=-1)
            print(f"Disliked filter applied. Problems: {problems.count()}")
        if favorited:
            # Debug: Check if there are any FavoriteProblem entries for the user
            favorite_entries = FavoriteProblem.objects.filter(user=request.user)
            print(f"FavoriteProblem entries for user {request.user.username}: {favorite_entries.count()}")
            if favorite_entries.exists():
                print(f"Favorited problems IDs: {[fav.problem_id for fav in favorite_entries]}")
            problems = problems.filter(favorited_by__user=request.user)
            print(f"Favorited filter applied. Problems: {problems.count()}")

    all_tags = Tag.objects.all()
    return render(request, 'problem_list.html', {
        'problems': problems,
        'search_query': search_query,
        'selected_tags': selected_tags,
        'all_tags': all_tags,
    })


def problem_detail(request, problem_id):
    problem = get_object_or_404(Problem, id=problem_id)
    
    # Get user-specific data only if authenticated
    user_rating = {'vote': 0}  # Default value
    user_solution = None
    solutions = []
    is_favorited = False
    
    if request.user.is_authenticated:
        user_rating = problem.ratings.filter(user=request.user).first() or {'vote': 0}
        user_solution = Solution.objects.filter(problem=problem, created_by=request.user).order_by('-created_at').first()
        solutions = Solution.objects.filter(problem=problem, created_by=request.user).order_by('-created_at')
        is_favorited = problem.favorited_by.filter(user=request.user).exists()
    
    # Get other users' solutions (exclude current user's solutions only if authenticated)
    if request.user.is_authenticated:
        other_solutions = Solution.objects.filter(problem=problem).exclude(created_by=request.user).order_by('-created_at')
    else:
        other_solutions = Solution.objects.filter(problem=problem).order_by('-created_at')
    
    # Get likes and dislikes
    likes = problem.ratings.filter(vote=1).count()
    dislikes = problem.ratings.filter(vote=-1).count()
    
    # Prepare context
    context = {
        'problem': problem,
        'likes': likes,
        'dislikes': dislikes,
        'user_rating': user_rating,
        'solutions': solutions,
        'other_solutions': other_solutions,
        'is_favorited': is_favorited,
        'user_solution': user_solution,
        'function_header': problem.function_header,
        'code': user_solution.code if user_solution else None,
        'input_vars': problem.input_vars,
        'return_type': problem.return_type,
        'results': None,
        'all_tests_passed': False,
    }
    
    # Handle solution submission
    if request.method == 'POST' and ('run' in request.POST or 'submit' in request.POST):
        if not request.user.is_authenticated:
            messages.error(request, "You must be logged in to submit a solution.")
            return redirect('login')
        
        code = request.POST.get('code')
        if not code:
            context['error'] = "Solution code cannot be empty."
        else:
            # Process the solution (run tests or submit)
            # This part depends on your existing logic for running tests or submitting
            # For now, I'll assume you have a function to handle this
            results, all_passed = run_tests(problem, code)  # Placeholder; replace with actual logic
            context['results'] = results
            context['all_tests_passed'] = all_passed
            
            if 'submit' in request.POST and all_passed:
                solution = Solution.objects.create(
                    problem=problem,
                    created_by=request.user,
                    code=code,
                    status='Accepted' if all_passed else 'Failed'
                )
                if all_passed:
                    problem.solved_by.add(request.user)
                problem.attempted_by.add(request.user)
                return redirect('problem_detail', problem_id=problem.id)
    
    return render(request, 'problem_detail.html', context)

def create_problem(request):
    if request.method == 'POST':
        print(f"Full POST data: {request.POST}")
        if 'generate_header' in request.POST:
            problem_form = ProblemForm(request.POST)
            input_vars = []
            i = 0
            while True:
                name_key = f'input_name_{i}'
                type_key = f'input_type_{i}'
                name = request.POST.get(name_key)
                var_type = request.POST.get(type_key)
                print(f"Checking {name_key}: {name}, {type_key}: {var_type}")
                if name is None and var_type is None:
                    break
                if name and var_type:
                    input_vars.append({'name': name, 'type': var_type})
                i += 1
            return_type = request.POST.get('return_type', 'None')
            function_header = generate_function_header(input_vars, return_type)
            
            selected_tags = request.POST.getlist('tags')
            new_tags = request.POST.get('new_tags', '').split(',')
            new_tags = [tag.strip() for tag in new_tags if tag.strip()]
            
            test_case_formset = TestCaseFormSet()
            problem_form = ProblemForm(request.POST, initial={'solution_code': function_header})
            print(f"Generated header: {function_header}, input_vars: {input_vars}")
            return render(request, 'create_problem.html', {
                'problem_form': problem_form,
                'test_case_formset': test_case_formset,
                'input_vars': input_vars,
                'return_type': return_type,
                'function_header': function_header,
                'header_generated': True,
                'selected_tags': selected_tags,
                'new_tags': new_tags,
                'all_tags': Tag.objects.all()
            })

        elif 'run' in request.POST or 'save' in request.POST:
            problem_form = ProblemForm(request.POST)
            test_case_formset = TestCaseFormSet(request.POST)
            input_vars_raw = request.POST.get('input_vars', '[]')
            print(f"Raw input_vars: {input_vars_raw}")
            try:
                input_vars = json.loads(input_vars_raw)
                if not isinstance(input_vars, list):
                    input_vars = []
            except json.JSONDecodeError:
                input_vars = []
                print("Invalid input_vars JSON:", input_vars_raw)
            print(f"Parsed input_vars: {input_vars}")

            return_type = request.POST.get('return_type', 'None')
            function_header = generate_function_header(input_vars, return_type)

            selected_tags = request.POST.getlist('tags')
            new_tags = request.POST.get('new_tags', '').split(',')
            new_tags = [tag.strip() for tag in new_tags if tag.strip()]
            print(f"POST data: {request.POST}")

            test_cases = []
            test_case_data = []
            total_forms = int(request.POST.get('form-TOTAL_FORMS', 0))
            max_index = max([int(k.split('-')[1]) for k in request.POST.keys() if k.startswith('form-') and 'param_' in k] + [total_forms - 1], default=0)
            print(f"Max test case index: {max_index + 1}")
            for i in range(max_index + 1):
                input_dict = {}
                test_case_input = {}
                for var in input_vars:
                    param_values = request.POST.getlist(f'form-{i}-param_{var["name"]}')
                    print(f"Test case {i} - {var['name']} values: {param_values}")
                    if param_values:
                        value = param_values[0].strip()
                        if var['type'] in ['dict', 'list']:
                            if value.startswith(f"{var['name']}:"):
                                value = value[len(var['name']) + 1:].strip()
                            try:
                                parsed_value = json.loads(value)
                                input_dict[var['name']] = parsed_value
                            except json.JSONDecodeError:
                                input_dict[var['name']] = value
                        elif var['type'] == 'bool':
                            input_dict[var['name']] = value.lower() == 'true'
                        elif var['type'] == 'int':
                            input_dict[var['name']] = int(value)
                        elif var['type'] == 'float':
                            input_dict[var['name']] = float(value)
                        elif var['type'] == 'None':
                            input_dict[var['name']] = None if value.lower() == 'null' else value
                        else:
                            input_dict[var['name']] = value
                        test_case_input[var['name']] = param_values[0]
                expected_values = request.POST.getlist(f'form-{i}-expected_output')
                print(f"Test case {i} - expected_values: {expected_values}")
                if input_dict and expected_values:
                    expected_output = expected_values[0]
                    if return_type in ['list', 'dict']:
                        try:
                            expected_output = json.loads(expected_output)
                        except json.JSONDecodeError:
                            pass
                    test_cases.append(type('TestCase', (), {
                        'input_value': json.dumps(input_dict),
                        'expected_output': expected_output,
                        'return_type': return_type
                    }))
                    test_case_data.append({
                        'inputs': test_case_input,
                        'expected_output': expected_values[0]
                    })
            print(f"Test cases: {test_cases}")
            print(f"Test case_data: {test_case_data}")

            if problem_form.is_valid():
                solution_code = request.POST.get('solution_code', '')

                if 'run' in request.POST:
                    print(f"Solution code: {solution_code}")
                    results = run_code_in_docker(solution_code, test_cases, input_vars)
                    all_tests_passed = all(result.get('passed', False) for result in results) and len(results) == len(test_cases)
                    request.session['last_run_results'] = results
                    print(f"Results from run_code_in_docker: {results}, all_tests_passed: {all_tests_passed}")
                    return render(request, 'create_problem.html', {
                        'problem_form': problem_form,
                        'test_case_formset': test_case_formset,
                        'results': results,
                        'solution_code': solution_code,
                        'input_vars': input_vars,
                        'return_type': return_type,
                        'function_header': function_header,
                        'header_generated': True,
                        'test_case_data': test_case_data,
                        'all_tests_passed': all_tests_passed,
                        'selected_tags': selected_tags,
                        'new_tags': new_tags,
                        'all_tags': Tag.objects.all()
                    })
                elif 'save' in request.POST:
                    print(f"Re-running tests before save with solution code: {solution_code}")
                    results = run_code_in_docker(solution_code, test_cases, input_vars)
                    all_tests_passed = all(result.get('passed', False) for result in results) and len(results) == len(test_cases)
                    if all_tests_passed:
                        print("Attempting to save problem")
                        problem = problem_form.save(commit=False)
                        problem.created_by = request.user
                        problem.input_vars = input_vars
                        problem.return_type = return_type
                        problem.function_header = function_header
                        problem.solution_code = solution_code
                        problem.save()
                        
                        for tag_name in selected_tags:
                            tag, _ = Tag.objects.get_or_create(name=tag_name.strip())
                            problem.tags.add(tag)
                        for tag_name in new_tags:
                            tag, _ = Tag.objects.get_or_create(name=tag_name.strip())
                            problem.tags.add(tag)
                        
                        for tc in test_cases:
                            TestCase.objects.create(
                                problem=problem,
                                input_value=tc.input_value,
                                expected_output=tc.expected_output
                            )
                        print(f"Problem saved with ID: {problem.id} and {len(test_cases)} test cases")
                        if 'last_run_results' in request.session:
                            del request.session['last_run_results']
                        return redirect('problem_detail', problem_id=problem.id)
                    else:
                        print("Cannot save: Not all tests passed with current test cases")
                        return render(request, 'create_problem.html', {
                            'problem_form': problem_form,
                            'test_case_formset': test_case_formset,
                            'results': results,
                            'solution_code': solution_code,
                            'input_vars': input_vars,
                            'return_type': return_type,
                            'function_header': function_header,
                            'header_generated': True,
                            'test_case_data': test_case_data,
                            'all_tests_passed': False,
                            'error': 'All test cases must pass with the current configuration before submitting.',
                            'selected_tags': selected_tags,
                            'new_tags': new_tags,
                            'all_tags': Tag.objects.all()
                        })
            print("Form errors:", problem_form.errors, test_case_formset.errors)
            return render(request, 'create_problem.html', {
                'problem_form': problem_form,
                'test_case_formset': test_case_formset,
                'input_vars': input_vars,
                'return_type': return_type,
                'function_header': function_header,
                'header_generated': True,
                'test_case_data': test_case_data,
                'error': 'Please correct the errors in the form.',
                'selected_tags': selected_tags,
                'new_tags': new_tags,
                'all_tags': Tag.objects.all()
            })
    else:
        if 'last_run_results' in request.session:
            del request.session['last_run_results']
        problem_form = ProblemForm()
        return render(request, 'create_problem.html', {
            'problem_form': problem_form,
            'input_vars': [],
            'return_type': 'None',
            'header_generated': False,
            'all_tags': Tag.objects.all()
        })

@login_required
def submit_solution(request, problem_id):
    problem = get_object_or_404(Problem, id=problem_id)
    function_header = problem.function_header
    test_cases = problem.test_cases.all()
    user_solution = Solution.objects.filter(problem=problem, created_by=request.user).order_by('-created_at').first()
    initial_code = user_solution.code if user_solution else function_header
    solutions = Solution.objects.filter(problem=problem, created_by=request.user) if request.user.is_authenticated else []
    other_solutions = Solution.objects.filter(problem=problem).exclude(created_by=request.user) if request.user.is_authenticated else Solution.objects.filter(problem=problem)
    likes = problem.ratings.filter(vote=1).count()
    dislikes = problem.ratings.filter(vote=-1).count()

    context = {
        'problem': problem,
        'function_header': function_header,
        'code': initial_code,
        'input_vars': problem.input_vars,
        'return_type': problem.return_type,
        'solutions': solutions,
        'other_solutions': other_solutions,
        'likes': likes,
        'dislikes': dislikes,
    }

    if request.method == 'POST':
        code = request.POST.get('code', '').strip()
        context['code'] = code
        print(f"POST request received. Code: {code if code else 'None'}")
        
        if not code:
            print("No code provided")
            context['error'] = 'Please enter code to run or submit.'
            return render(request, 'problem_detail.html', context)

        test_cases_with_return_type = [
            type('TestCase', (), {
                'input_value': tc.input_value,
                'expected_output': tc.expected_output,
                'return_type': problem.return_type
            }) for tc in test_cases
        ]
        print(f"Test cases loaded: {len(test_cases_with_return_type)}")

        if not test_cases_with_return_type:
            print("No test cases available for this problem")
            context['error'] = 'No test cases defined for this problem.'
            return render(request, 'problem_detail.html', context)

        if request.user not in problem.attempted_by.all():
            problem.attempted_by.add(request.user)
            print(f"User {request.user.username} marked as attempted problem {problem.id} for the first time")

        if 'run' in request.POST:
            print(f"Run button clicked. Executing code:\n{code}")
            try:
                results = run_code_in_docker(code, test_cases_with_return_type, problem.input_vars)
                print(f"Raw results from run_code_in_docker: {results}")
                all_tests_passed = all(result.get('passed', False) for result in results) if results else False
                if not results:
                    print("No results returned from run_code_in_docker")
                    context['error'] = 'No test results generated. Check your code or test cases.'
                    return render(request, 'problem_detail.html', context)
                context.update({
                    'results': results,
                    'all_tests_passed': all_tests_passed,
                })
                print(f"Processed results: {results}, all_tests_passed: {all_tests_passed}")
                return render(request, 'problem_detail.html', context)
            except Exception as e:
                print(f"Error during code execution: {str(e)}")
                context['error'] = f"Failed to run code: {str(e)}"
                return render(request, 'problem_detail.html', context)
        elif 'submit' in request.POST:
            print(f"Submit button clicked. Executing code:\n{code}")
            try:
                results = run_code_in_docker(code, test_cases_with_return_type, problem.input_vars)
                all_tests_passed = all(result.get('passed', False) for result in results) if results else False
                print(f"Results from run_code_in_docker: {results}, all_tests_passed: {all_tests_passed}")
                if all_tests_passed:
                    if user_solution:
                        user_solution.code = code
                        user_solution.save()
                        print("Updated existing solution")
                    else:
                        Solution.objects.create(problem=problem, code=code, created_by=request.user)
                        print("Created new solution")
                    if request.user not in problem.solved_by.all():
                        problem.solved_by.add(request.user)
                        print(f"User {request.user.username} marked as solved problem {problem.id} for the first time")
                    return redirect('problem_detail', problem_id=problem.id)
                context.update({
                    'results': results,
                    'all_tests_passed': all_tests_passed,
                    'error': 'Solution failed some test cases.',
                })
                return render(request, 'problem_detail.html', context)
            except Exception as e:
                print(f"Error during submission: {str(e)}")
                context['error'] = f"Error submitting code: {str(e)}"
                return render(request, 'problem_detail.html', context)
    else:
        print("GET request to submit_solution")
        return render(request, 'problem_detail.html', context)

def profile(request):
    target_username = request.GET.get('user')
    
    if target_username:
        target_user = get_object_or_404(User, username=target_username)
        profile, created = Profile.objects.get_or_create(user=target_user)
        problems = Problem.objects.filter(created_by=target_user)
        solutions = Solution.objects.filter(created_by=target_user)
        form = None
    else:
        target_user = request.user
        profile, created = Profile.objects.get_or_create(user=target_user)
        problems = Problem.objects.filter(created_by=target_user)
        solutions = Solution.objects.filter(created_by=target_user)
        if request.method == 'POST':
            form = ProfileForm(request.POST, request.FILES, instance=profile)
            if form.is_valid():
                form.save()
                print(f"Profile picture updated for {target_user.username}")
                return redirect('profile')
        else:
            form = ProfileForm(instance=profile)
    
    return render(request, 'profile.html', {
        'target_user': target_user,
        'profile': profile,
        'problems': problems,
        'solutions': solutions,
        'form': form,
    })

def rate_problem(request, problem_id):
    problem = get_object_or_404(Problem, id=problem_id)
    print(f"Rate problem {problem_id} by {request.user.username}")
    if request.method == 'POST':
        vote = request.POST.get('vote')
        print(f"Received vote: {vote}")
        try:
            vote = int(vote)
            if vote not in [1, -1, 0]:
                print("Invalid vote value")
                return JsonResponse({'error': 'Invalid vote'}, status=400)
            
            rating = ProblemRating.objects.filter(problem=problem, user=request.user).first()
            if vote == 0:
                if rating:
                    rating.delete()
                    print(f"Rating removed for user {request.user.username}")
                user_vote = 0
            else:
                rating, created = ProblemRating.objects.get_or_create(
                    problem=problem,
                    user=request.user,
                    defaults={'vote': vote}
                )
                if not created:
                    rating.vote = vote
                    rating.save()
                print(f"Rating saved: vote={vote}, created={created}")
                user_vote = vote
            
            likes = problem.ratings.filter(vote=1).count()
            dislikes = problem.ratings.filter(vote=-1).count()
            response = {
                'likes': likes,
                'dislikes': dislikes,
                'net_rating': likes - dislikes,
                'user_vote': user_vote,
            }
            print(f"Returning response: {response}")
            return JsonResponse(response)
        except ValueError:
            print("Vote could not be converted to int")
            return JsonResponse({'error': 'Vote must be an integer'}, status=400)
        except Exception as e:
            print(f"Error saving rating: {str(e)}")
            return JsonResponse({'error': f'Server error: {str(e)}'}, status=500)
    print("Not a POST request")
    return JsonResponse({'error': 'Invalid request'}, status=400)

def toggle_favorite(request, problem_id):
    problem = get_object_or_404(Problem, id=problem_id)
    if request.method == 'POST':
        favorite, created = FavoriteProblem.objects.get_or_create(
            problem=problem,
            user=request.user
        )
        if not created:
            favorite.delete()
            is_favorited = False
        else:
            is_favorited = True
        return JsonResponse({'is_favorited': is_favorited})
    return JsonResponse({'error': 'Invalid request'}, status=400)

def delete_problem(request, problem_id):
    problem = get_object_or_404(Problem, id=problem_id)
    if request.user != problem.created_by:
        return redirect('problem_detail', problem_id=problem.id)
    if request.method == 'POST':
        problem.delete()
        return redirect('problem_list')
    return redirect('problem_detail', problem_id=problem.id)